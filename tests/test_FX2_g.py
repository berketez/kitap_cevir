"""FX2 (5 Ekim 2026): Marker'ın düşürdüğü/bozduğu içeriği kaynak PDF'ten kurtarma.

Kalemler: kayıp metin, kesilmiş grafik başlıkları, matematikte ondalık
virgül (mt okuma MT-14), display matematikte ok
simgesi (MT-46), Rusça satır içi matematik artığı (RAPOR MT-8), tablo
hücresindeki düz rakam dipnot işareti.

Bütün olgular SENTETİK cümlelerle kurulur (KARAR 26: açık yayında kitap metni
yok); küçük PDF'ler PyMuPDF ile testin içinde üretilir.
"""
from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

fitz = pytest.importorskip("fitz")
np = pytest.importorskip("numpy")
pytest.importorskip("cv2")
Image = pytest.importorskip("PIL.Image")

from latex_pipeline import sekil_kirpma as sk  # noqa: E402

OLCEK = sk.MARKER_DPI / 72.0

# Benzersiz sözcüklü sentetik gövde metni.
UST_METIN = [
    "Copper gliders drift above the quiet orchard every winter morning.",
    "A patient cartographer labels seven islands with violet ink.",
]
ALT_METIN = [
    "Marble owls guard the archive while brass pendulums keep time.",
    "Every lighthouse keeper records the tide in a striped notebook.",
]
ALTYAZI = "Figure 1: Synthetic chart of imaginary harbour traffic."


def _raster_png(genislik=800, yukseklik=500, baslik=True, logo=True,
                govde=True, cubuk_sinir=700) -> bytes:
    """Başlık bandı (üstte), çizim gövdesi ve logo (altta) olan raster."""
    a = np.full((yukseklik, genislik, 3), 255, dtype=np.uint8)
    if baslik:
        a[20:55, 60:520] = (20, 20, 20)          # "başlık" yazısı yerine koyu bant
        a[62:80, 60:380] = (90, 90, 90)          # "alt başlık"
    if govde:
        a[120:400, 80:84] = (0, 0, 0)            # y ekseni
        a[396:400, 80:min(740, cubuk_sinir + 40)] = (0, 0, 0)   # x ekseni
        for i, x in enumerate(range(120, cubuk_sinir, 80)):
            a[400 - 30 * (i + 1):396, x:x + 40] = (40, 90, 160)   # çubuklar
    if logo:
        a[440:480, 600:760] = (30, 120, 60)      # logo
    buf = io.BytesIO()
    Image.fromarray(a).save(buf, format="PNG")
    return buf.getvalue()


def _pdf_ve_kirpim(tmp_path: Path, raster_kutu=(100, 200, 500, 450),
                   kirpim=(110, 240, 490, 420), png=None, ekle=None,
                   ad="_page_0_Figure_1.png", sayfa_ekle=None):
    """Tek sayfalık PDF + Marker benzeri kırpım (sayfa çiziminden) üret."""
    doc = fitz.open()
    page = doc.new_page(width=595, height=842)
    y = 72
    for t in UST_METIN:
        page.insert_text((72, y), t, fontsize=11)
        y += 16
    page.insert_image(fitz.Rect(*raster_kutu), stream=png or _raster_png())
    if ekle:
        ekle(page)
    y = raster_kutu[3] + 24
    page.insert_text((72, y), ALTYAZI, fontsize=9)
    y += 30
    for t in ALT_METIN:
        page.insert_text((72, y), t, fontsize=11)
        y += 16
    if sayfa_ekle:
        sayfa_ekle(doc)
    pdf = tmp_path / "source_book.pdf"
    doc.save(str(pdf))
    doc.close()
    img_dir = tmp_path / "images"
    img_dir.mkdir(exist_ok=True)
    _kirp_kaydet(pdf, 0, kirpim, img_dir / ad)
    return str(pdf), str(img_dir)


def _kirp_kaydet(pdf, sayfa, kirpim, hedef):
    doc = fitz.open(str(pdf))
    pix = doc[sayfa].get_pixmap(matrix=fitz.Matrix(OLCEK, OLCEK), alpha=False)
    a = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, pix.n)
    x0, y0, x1, y1 = (int(round(v * OLCEK)) for v in kirpim)
    Image.fromarray(a[y0:y1, x0:x1, :3].copy()).save(str(hedef))
    doc.close()


def _md(*gorseller):
    satirlar = UST_METIN[:]
    for g in gorseller:
        satirlar.append(f"![]({g})")
    satirlar.append("*" + ALTYAZI + "*")
    satirlar += ALT_METIN
    return "\n\n".join(satirlar) + "\n"


def _koyu_satir_var(img: Path, y0: int, y1: int) -> bool:
    a = np.asarray(Image.open(img).convert("L"))
    return bool((a[y0:y1] < 120).any())


# ---------------------------------------------------------------------------
# Kalem 2: kesilmiş grafik başlıkları
# ---------------------------------------------------------------------------

def test_kesik_raster_baslik_ve_logo_geri_gelir(tmp_path):
    pdf, img_dir = _pdf_ve_kirpim(tmp_path)
    gorsel = Path(img_dir) / "_page_0_Figure_1.png"
    eski = Image.open(gorsel).size
    md = _md("_page_0_Figure_1.jpeg")      # Marker .jpeg yazıp .png kaydeder
    yeni_md, rapor = sk.onar_raporlu(md, pdf, img_dir)
    assert yeni_md == md
    assert len(rapor.genisletilen) == 1
    g = rapor.genisletilen[0]
    # Yeni kutu rasterin tamamını kapsar ve eskisini içerir.
    assert g.yeni[1] <= 200.5 and g.yeni[3] >= 449.5
    assert g.yeni[0] <= g.kirpim[0] and g.yeni[2] >= g.kirpim[2]
    w, h = Image.open(gorsel).size
    assert h > eski[1] and w >= eski[0]
    # Başlık bandı (rasterin 20-55. satırları ≈ 8-22 pt) artık görselde.
    assert _koyu_satir_var(gorsel, int(8 * OLCEK), int(22 * OLCEK))
    # Özgün yedeklendi, kayıt yazıldı.
    assert (Path(img_dir) / sk.YEDEK_KLASORU / "_page_0_Figure_1.png").is_file()
    kayit = json.loads((Path(img_dir) / sk.KAYIT_DOSYASI).read_text(encoding="utf-8"))
    assert "_page_0_Figure_1.png" in kayit


def test_ikinci_kosuda_dokunulmaz(tmp_path):
    pdf, img_dir = _pdf_ve_kirpim(tmp_path)
    md = _md("_page_0_Figure_1.png")
    sk.onar_raporlu(md, pdf, img_dir)
    gorsel = Path(img_dir) / "_page_0_Figure_1.png"
    once = gorsel.read_bytes()
    _, rapor = sk.onar_raporlu(md, pdf, img_dir)
    assert gorsel.read_bytes() == once
    assert rapor.atlanan.get("zaten_genisletilmis") == 1
    assert rapor.genisletilen and rapor.genisletilen[0].nedenler == ["onceki_kosu"]


def test_yalniz_beyaz_kenar_kesikse_dokunulmaz(tmp_path):
    # Raster kenarları beyaz; kırpım yalnız beyaz kısmı dışarıda bırakıyor.
    png = _raster_png(baslik=False, logo=False)
    pdf, img_dir = _pdf_ve_kirpim(tmp_path, png=png, kirpim=(118, 236, 492, 445))
    gorsel = Path(img_dir) / "_page_0_Figure_1.png"
    once = gorsel.read_bytes()
    yeni_md, rapor = sk.onar_raporlu(_md("_page_0_Figure_1.png"), pdf, img_dir)
    assert not rapor.genisletilen
    assert gorsel.read_bytes() == once
    assert rapor.atlanan.get("serit_bos", 0) >= 1
    assert not (Path(img_dir) / sk.YEDEK_KLASORU).exists()


def test_seritte_govde_metni_varsa_dokunulmaz(tmp_path):
    # Rasterin kesik üst şeridinin üstüne Markdown'da da bulunan bir gövde
    # satırı basılmış: şerit genişletilirse metin görselde yinelenirdi.
    satir = "Quiet bronze kettles hum beside the silver window."

    def ekle(page):
        page.insert_text((104, 214), satir, fontsize=11)

    png = _raster_png(baslik=False, logo=False)
    pdf, img_dir = _pdf_ve_kirpim(tmp_path, png=png, ekle=ekle,
                                  kirpim=(100, 240, 500, 450))
    md = _md("_page_0_Figure_1.png").replace(ALTYAZI, ALTYAZI + " " + satir)
    _, rapor = sk.onar_raporlu(md, pdf, img_dir)
    assert not rapor.genisletilen
    assert rapor.atlanan.get("serit_govde_metni", 0) >= 1


def test_taranmis_sayfa_rasteri_genisletmez(tmp_path):
    # Bütün sayfayı kaplayan raster (taranmış sayfa): şekil değil zemin.
    png = _raster_png(genislik=600, yukseklik=850)
    pdf, img_dir = _pdf_ve_kirpim(tmp_path, png=png, raster_kutu=(0, 0, 595, 842),
                                  kirpim=(110, 240, 490, 420))
    _, rapor = sk.onar_raporlu(_md("_page_0_Figure_1.png"), pdf, img_dir)
    assert not rapor.genisletilen


def test_ayri_kirpilmis_logo_parcasi_kaldirilir(tmp_path):
    # Marker grafiği (başlık ve logo dışarıda) ve logoyu ayrı ayrı kırpmış.
    pdf, img_dir = _pdf_ve_kirpim(tmp_path, kirpim=(110, 240, 490, 420))
    # Logo kırpımı rasterin beyaz kenarına da taşıyor (bir rapordaki gibi): ana
    # kırpımın sağ kenarı genişlemediği için parça onun 5 pt dışına çıkıyor.
    logo_pt = (100 + 590 * 0.5, 200 + 436 * 0.5, 495, 200 + 484 * 0.5)
    _kirp_kaydet(pdf, 0, logo_pt, Path(img_dir) / "_page_0_Figure_2.png")
    md = _md("_page_0_Figure_1.jpeg", "_page_0_Figure_2.jpeg")
    yeni_md, rapor = sk.onar_raporlu(md, pdf, img_dir)
    assert "_page_0_Figure_1.jpeg" in yeni_md
    assert "_page_0_Figure_2" not in yeni_md
    assert [g.ad for g in rapor.kaldirilan_parcalar] == ["_page_0_Figure_2.png"]
    # Parça dosyasına dokunulmadı (yalnız Markdown satırı çıktı); metin aynen.
    assert yeni_md.replace("\n", "") == md.replace("![](_page_0_Figure_2.jpeg)", "").replace("\n", "")


def test_kesik_vektor_dugum_genisler_altyaziya_tasmaz(tmp_path):
    # Raster yok: vektör çizim. Alttaki düğüm (daire) kırpım çizgisinde
    # kesilmiş; büyük çerçeve ise altyazıya kadar uzanıyor.
    def ekle(page):
        page.draw_rect(fitz.Rect(150, 260, 450, 400), color=(0, 0, 0), width=1.5)
        page.draw_circle((300, 412), 14, color=(0.2, 0.2, 0.2), fill=(0.3, 0.3, 0.3))

    png = _raster_png(baslik=False, logo=False, govde=False)

    def sayfa_ekle(doc):
        pass

    pdf, img_dir = _pdf_ve_kirpim(tmp_path, png=png, ekle=ekle,
                                  raster_kutu=(560, 800, 561, 801),
                                  kirpim=(140, 250, 460, 415))
    _, rapor = sk.onar_raporlu(_md("_page_0_Figure_1.png"), pdf, img_dir)
    assert len(rapor.genisletilen) == 1
    g = rapor.genisletilen[0]
    assert g.yeni[3] >= 425.5            # dairenin altı (412 + 14)
    assert g.yeni[3] < 470               # altyazı satırına (474) inmez
    assert any(n.startswith("cizim:alt") for n in g.nedenler)


def test_pdf_ya_da_klasor_yoksa_aynen(tmp_path):
    md = _md("_page_0_Figure_1.png")
    assert sk.onar(md, None, str(tmp_path)) is md
    assert sk.onar(md, str(tmp_path / "yok.pdf"), str(tmp_path)) is md
    pdf, img_dir = _pdf_ve_kirpim(tmp_path)
    assert sk.onar(md, pdf, str(tmp_path / "yok")) is md


def test_hata_olursa_aynen(tmp_path, monkeypatch):
    def patla(*_a, **_k):
        raise RuntimeError("beklenmeyen")
    monkeypatch.setattr(sk, "onar_raporlu", patla)
    md = _md("_page_0_Figure_1.png")
    assert sk.onar(md, str(tmp_path / "a.pdf"), str(tmp_path)) is md


def test_sekil_bolgeleri_raporu(tmp_path):
    pdf, img_dir = _pdf_ve_kirpim(tmp_path)
    _, rapor = sk.onar_raporlu(_md("_page_0_Figure_1.png"), pdf, img_dir, yaz=False)
    bolge = rapor.sekil_bolgeleri()
    assert list(bolge) == [0]
    x0, y0, x1, y1 = bolge[0][0]
    assert y0 <= 200.5 and y1 >= 449.5
    # yaz=False: dosyaya dokunulmadı.
    assert not (Path(img_dir) / sk.KAYIT_DOSYASI).exists()


def test_vektor_altyazi_satirina_tasmaz_md_de_bulunmasa_da(tmp_path):
    # Harita benzeri vektör çizim kırpımın altından altyazı satırına uzanıyor;
    # altyazı satırı tireyle bölünmüş olduğundan Markdown'da aynen bulunmaz.
    uzun = "Map 2: Imaginary coastline of seven islands with bridges and fer-"

    def ekle(page):
        page.draw_rect(fitz.Rect(120, 250, 480, 470), color=(0.1, 0.1, 0.4),
                       fill=(0.75, 0.82, 0.95), width=1.2)
        page.insert_text((124, 462), uzun, fontsize=9)

    png = _raster_png(baslik=False, logo=False, govde=False)
    pdf, img_dir = _pdf_ve_kirpim(tmp_path, png=png, ekle=ekle,
                                  raster_kutu=(560, 800, 561, 801),
                                  kirpim=(110, 240, 490, 450))
    md = _md("_page_0_Figure_1.png").replace(
        ALTYAZI, "Map 2: Imaginary coastline of seven islands with bridges and ferries.")
    _, rapor = sk.onar_raporlu(md, pdf, img_dir)
    assert not rapor.genisletilen
    assert rapor.atlanan.get("serit_govde_metni", 0) >= 1


def test_dar_cizim_seridi_ama_bant_altyaziyi_kesiyor(tmp_path):
    # Dar bir çizim (ok gövdesi) kırpımın altına uzanıyor; kendi şeridi dar
    # ama kutu dikdörtgen olduğundan alt bant bütün genişlikte görsele girer
    # ve altyazı satırını keser: genişletilmez.
    def ekle(page):
        page.draw_rect(fitz.Rect(300, 300, 312, 470), color=(0, 0, 0), fill=(0, 0, 0))
        page.insert_text((124, 462), "Map 3: Pale violet routes between the seven "
                         "imaginary islands of the eastern archipelago.", fontsize=9)

    png = _raster_png(baslik=False, logo=False, govde=False)
    pdf, img_dir = _pdf_ve_kirpim(tmp_path, png=png, ekle=ekle,
                                  raster_kutu=(560, 800, 561, 801),
                                  kirpim=(110, 240, 490, 450))
    _, rapor = sk.onar_raporlu(_md("_page_0_Figure_1.png"), pdf, img_dir)
    assert not rapor.genisletilen


def test_kosedeki_logo_da_alinir(tmp_path):
    # Logo rasterin sağ alt köşesinde; kırpım hem altı hem sağı kesiyor.
    # Alt şerit genişleyince logonun sağ ucu yan şeride girer: o da alınır.
    # Çubuklar soldan 560. piksele dek: sağ şeritte (kırpımın dikey
    # aralığında) mürekkep yok, mürekkep yalnız köşede (logo).
    png = _raster_png(cubuk_sinir=560)
    pdf, img_dir = _pdf_ve_kirpim(tmp_path, png=png, kirpim=(110, 240, 440, 420))
    _, rapor = sk.onar_raporlu(_md("_page_0_Figure_1.png"), pdf, img_dir)
    g = rapor.genisletilen[0]
    assert g.yeni[3] >= 449.5            # logo (rasterde 440-480. satırlar) altta
    assert g.yeni[2] >= 479.5            # logonun sağ ucu (760 px -> 480 pt)


# ---------------------------------------------------------------------------
# Kalem 1: kayıp metin
# ---------------------------------------------------------------------------

from latex_pipeline import kayip_kurtarma as kk  # noqa: E402

GOVDE = [
    "The northern ferry leaves at dawn and carries salt to the distant valley.",
    "Several weavers repair the old sails while the harbour master counts crates.",
    "A narrow bridge connects the market square with the quiet copper district.",
    "During the rainy months the lantern makers move their workshops indoors.",
    "Children collect smooth pebbles near the river and sort them by colour.",
    "The archive keeps every shipping ledger since the founding of the town.",
    "Merchants from the eastern coast bring dried figs and bolts of linen cloth.",
    "At noon the bell tower rings twice to announce the arrival of the mail boat.",
    "Old fishermen repair their nets on the pier and argue about the weather.",
    "The glassblower shapes delicate vials for the apothecary near the gate.",
    "A wandering cartographer sketches the coastline from the lighthouse balcony.",
    "Every spring the guild elects a new treasurer by drawing coloured stones.",
]
GOVDE2 = [
    "Beyond the hills a shepherd guides his flock toward the winter pastures.",
    "The miller grinds barley for the bakers who supply the garrison kitchens.",
    "Traders exchange news of storms and pirates in the crowded tavern hall.",
    "A scholar copies faded charts in the library beneath the cathedral dome.",
    "When the tide turns the canal locks open and barges glide into the city.",
    "The night watch lights torches along the wall and checks every postern.",
    "Pilgrims rest at the abbey before continuing their journey to the shrine.",
    "The tanners work downstream so that the smell does not reach the square.",
    "In autumn the orchards are full of pickers carrying wicker baskets of fruit.",
    "Every evening the innkeeper writes the names of new guests in a ledger.",
    "The blacksmith forges hinges and nails for the shipwrights at the docks.",
    "Gulls circle above the fish market waiting for scraps from the stalls.",
]
BASLIK_A = "Harbour accounts"
BASLIK_KAYIP = "Signals from the lighthouse"
UST_BILGI = "Chronicle of a Coastal Town"


def _kayip_pdf(tmp_path: Path, sayfa1, sayfa2, baslik_sayfa1=None,
               baslik_sayfa1_basa=None, ust_bilgi=True, sayfa_no=True,
               ekle=None) -> str:
    doc = fitz.open()
    for pno, (satirlar, son_baslik, bas_baslik) in enumerate(
            ((sayfa1, baslik_sayfa1, baslik_sayfa1_basa), (sayfa2, None, None))):
        page = doc.new_page(width=595, height=842)
        if ust_bilgi:
            page.insert_text((72, 50), UST_BILGI, fontsize=9)
        y = 90
        if bas_baslik:
            page.insert_text((72, y), bas_baslik, fontsize=11, fontname="hebo")
            y += 22
        for t in satirlar:
            page.insert_text((72, y), t, fontsize=11)
            y += 15
        if son_baslik:
            y += 8
            page.insert_text((72, y), son_baslik, fontsize=11, fontname="hebo")
        if sayfa_no:
            page.insert_text((290, 810), str(pno + 1), fontsize=9)
        if ekle:
            ekle(doc, page, pno)
    yol = tmp_path / "kaynak.pdf"
    doc.save(str(yol))
    doc.close()
    return str(yol)


def _paragraf(satirlar):
    return " ".join(satirlar)


def test_sayfa_sonundaki_kayip_baslik_eklenir(tmp_path):
    # Sayfa 1: başta "Harbour accounts" başlığı (Markdown'da #### olarak var),
    # sonda aynı stilde "Signals from the lighthouse" başlığı (Marker düşürmüş).
    pdf = _kayip_pdf(tmp_path, GOVDE, GOVDE2, baslik_sayfa1=BASLIK_KAYIP,
                     baslik_sayfa1_basa=BASLIK_A)
    md = (f"#### {BASLIK_A}\n\n{_paragraf(GOVDE)}\n\n{_paragraf(GOVDE2)}\n")
    yeni, rapor = kk.onar_raporlu(md, pdf)
    assert len(rapor.eklemeler) == 1, (rapor.atlanan, rapor.eklenmeyen)
    e = rapor.eklemeler[0]
    assert e.bicim == "baslik"
    assert f"{_paragraf(GOVDE)}\n\n#### {BASLIK_KAYIP}\n\n{_paragraf(GOVDE2)}" in yeni
    # Sayfa numarası ve üst bilgi eklenmedi (KARAR 32).
    assert UST_BILGI not in yeni
    assert "\n1\n" not in yeni and "\n2\n" not in yeni


def test_ust_bilgi_ve_sayfa_numarasi_eklenmez(tmp_path):
    pdf = _kayip_pdf(tmp_path, GOVDE, GOVDE2)
    md = f"{_paragraf(GOVDE)}\n\n{_paragraf(GOVDE2)}\n"
    yeni, rapor = kk.onar_raporlu(md, pdf)
    assert yeni == md
    assert not rapor.eklemeler


def test_paragraf_icinde_kayip_satir_araya_girer(tmp_path):
    pdf = _kayip_pdf(tmp_path, GOVDE, GOVDE2)
    eksik = GOVDE[:5] + GOVDE[6:]          # 6. satır düşmüş
    md = f"{_paragraf(eksik)}\n\n{_paragraf(GOVDE2)}\n"
    yeni, rapor = kk.onar_raporlu(md, pdf)
    assert len(rapor.eklemeler) == 1, (rapor.atlanan, rapor.eklenmeyen)
    assert rapor.eklemeler[0].bicim == "satir_ici"
    assert f"{_paragraf(GOVDE)}\n\n{_paragraf(GOVDE2)}\n" == yeni


def test_baska_yerde_bulunan_satir_eklenmez(tmp_path):
    pdf = _kayip_pdf(tmp_path, GOVDE, GOVDE2)
    eksik = GOVDE[:5] + GOVDE[6:]
    # Düşen satır belgenin sonunda (yer değiştirmiş): kayıp değil.
    md = f"{_paragraf(eksik)}\n\n{_paragraf(GOVDE2)}\n\n{GOVDE[5]}\n"
    yeni, rapor = kk.onar_raporlu(md, pdf)
    assert yeni == md
    assert not rapor.eklemeler


def test_parcasi_olan_satir_eklenmez(tmp_path):
    pdf = _kayip_pdf(tmp_path, GOVDE, GOVDE2)
    eksik = GOVDE[:5] + GOVDE[6:]
    # Uzun satırın parçalarının çoğu başka yerde (Marker taşıyıp
    # matematikle bölmüş): yinelenme tehlikesi, eklenmez.
    md = (f"{_paragraf(eksik)}\n\n{_paragraf(GOVDE2)}\n\n"
          "Note: The archive keeps every shipping $x$ ledger since the founding of the town.\n")
    yeni, rapor = kk.onar_raporlu(md, pdf)
    assert yeni == md
    assert not rapor.eklemeler


def test_capalar_bitisik_degilse_eklenmez(tmp_path):
    pdf = _kayip_pdf(tmp_path, GOVDE, GOVDE2)
    eksik = GOVDE[:5] + ["A completely different sentence sits here instead."] + GOVDE[6:]
    md = f"{_paragraf(eksik)}\n\n{_paragraf(GOVDE2)}\n"
    yeni, rapor = kk.onar_raporlu(md, pdf)
    assert yeni == md
    assert rapor.atlanan.get("bitisik_degil") == 1


def test_sekil_bolgesindeki_yazi_eklenmez(tmp_path):
    pdf = _kayip_pdf(tmp_path, GOVDE, GOVDE2)
    eksik = GOVDE[:5] + GOVDE[6:]
    md = f"{_paragraf(eksik)}\n\n{_paragraf(GOVDE2)}\n"
    # 6. satır (y ≈ 90 + 5*15 = 165) bir şekil kırpımının içinde say.
    yeni, rapor = kk.onar_raporlu(md, pdf, {0: [(60, 150, 560, 172)]})
    assert yeni == md
    assert rapor.atlanan.get("sekil_ici") == 1


def test_kod_blogu_ve_acili_ayracli_metin_var_sayilir(tmp_path):
    satirlar = GOVDE[:6] + ["{Maybe <ask the keeper about the lamp> later}",
                            "zzSIGNAL_LAMP_ORANGE_CHECK"] + GOVDE[6:]
    pdf = _kayip_pdf(tmp_path, satirlar, GOVDE2)
    md = (f"{_paragraf(GOVDE[:6])}\n\n- {{Maybe <ask the keeper about the lamp> later}}\n\n"
          f"```\nzzSIGNAL\\_LAMP\\_ORANGE\\_CHECK\n```\n\n{_paragraf(GOVDE[6:])}\n\n"
          f"{_paragraf(GOVDE2)}\n")
    yeni, rapor = kk.onar_raporlu(md, pdf)
    assert yeni == md
    assert not rapor.eklemeler


def test_guvenlik_agi_tutmazsa_hic_eklenmez(tmp_path, monkeypatch):
    pdf = _kayip_pdf(tmp_path, GOVDE, GOVDE2)
    eksik = GOVDE[:5] + GOVDE[6:]
    md = f"{_paragraf(eksik)}\n\n{_paragraf(GOVDE2)}\n"
    monkeypatch.setattr(kk, "_satirlari_birlestir", lambda satirlar: "bozuk metin")
    yeni, rapor = kk.onar_raporlu(md, pdf)
    assert yeni == md
    assert rapor.atlanan.get("guvenlik_agi") == 1


def test_kayip_pdf_yoksa_aynen(tmp_path):
    md = "Some text."
    assert kk.onar(md, None) is md
    assert kk.onar(md, str(tmp_path / "yok.pdf")) is md


def test_kayip_hata_olursa_aynen(tmp_path, monkeypatch):
    def patla(*_a, **_k):
        raise RuntimeError("beklenmeyen")
    monkeypatch.setattr(kk, "onar_raporlu", patla)
    md = "Some text."
    assert kk.onar(md, str(tmp_path / "a.pdf")) is md


def test_cevrede_yer_degistirmis_bicimde_varsa_eklenmez(tmp_path):
    # PDF satırının sonundaki dipnot işareti Markdown'da başa taşınmış ve
    # satır önceki paragrafın önüne kaymış (bir rapordaki tablo hücresi gibi):
    # ekleme yerinin hemen gerisinde, imleç penceresinin dışında.
    kayan = "The lamp of the harbour burns until midnight 42"
    satirlar = GOVDE[:2] + [kayan] + GOVDE[2:]
    pdf = _kayip_pdf(tmp_path, satirlar, GOVDE2)
    md = (f"42 The lamp of the harbour burns until midnight\n\n{_paragraf(GOVDE)}\n\n"
          f"{_paragraf(GOVDE2)}\n")
    yeni, rapor = kk.onar_raporlu(md, pdf)
    assert yeni == md
    assert not rapor.eklemeler
    assert rapor.atlanan.get("yakinda_parca") == 1


def test_capalar_ters_sirada_ise_eklenmez(tmp_path):
    pdf = _kayip_pdf(tmp_path, GOVDE, GOVDE2)
    # Markdown'da sonraki çapa (GOVDE[6]) önceki çapadan (GOVDE[4]) önce.
    md = f"{_paragraf(GOVDE[6:])}\n\n{_paragraf(GOVDE[:5])}\n\n{_paragraf(GOVDE2)}\n"
    yeni, rapor = kk.onar_raporlu(md, pdf)
    assert yeni == md
    assert not rapor.eklemeler


def test_paragrafin_son_satiri_paragrafa_eklenir(tmp_path):
    # Paragrafın son satırı düşmüş, Markdown'da ardından paragraf sınırı
    # var: satır ayrı paragraf DEĞİL, önceki paragrafın sonuna girer.
    pdf = _kayip_pdf(tmp_path, GOVDE, GOVDE2)
    md = f"{_paragraf(GOVDE[:-1])}\n\n{_paragraf(GOVDE2)}\n"
    yeni, rapor = kk.onar_raporlu(md, pdf)
    assert len(rapor.eklemeler) == 1, (rapor.atlanan, rapor.eklenmeyen)
    assert rapor.eklemeler[0].bicim == "satir_ici"
    assert yeni == f"{_paragraf(GOVDE)}\n\n{_paragraf(GOVDE2)}\n"


def test_baglanti_hedefli_gorsel_satiri_da_islenir(tmp_path):
    # Marker görselin önüne bağlantı hedefi koyar: satır yine görsel satırıdır;
    # yinelenen parça kaldırılırken hedef korunur.
    pdf, img_dir = _pdf_ve_kirpim(tmp_path, kirpim=(110, 240, 490, 420))
    logo_pt = (100 + 590 * 0.5, 200 + 436 * 0.5, 495, 200 + 484 * 0.5)
    _kirp_kaydet(pdf, 0, logo_pt, Path(img_dir) / "_page_0_Figure_2.png")
    md = (_md("_page_0_Figure_1.jpeg", "_page_0_Figure_2.jpeg")
          .replace("![](_page_0_Figure_1.jpeg)", '<span id="page-0-1"></span>![](_page_0_Figure_1.jpeg)')
          .replace("![](_page_0_Figure_2.jpeg)", '<span id="page-0-2"></span>![](_page_0_Figure_2.jpeg)'))
    yeni_md, rapor = sk.onar_raporlu(md, pdf, img_dir)
    assert len(rapor.genisletilen) == 1
    assert [g.ad for g in rapor.kaldirilan_parcalar] == ["_page_0_Figure_2.png"]
    assert '<span id="page-0-2"></span>' in yeni_md
    assert "_page_0_Figure_2" not in yeni_md


# ---------------------------------------------------------------------------
# Kalem 3–6: PDF metin katmanıyla matematik ve tablo onarımı
# (latex_pipeline/pdf_metin_onarim.py)
# ---------------------------------------------------------------------------

from latex_pipeline import pdf_metin_onarim as pmo  # noqa: E402

UNICODE_FONT = Path("/System/Library/Fonts/Supplemental/Arial Unicode.ttf")
DOLGU = [
    "Quiet violet harbours shelter brass compasses during long winters.",
    "Seven amber lanterns guide the patient ferryman across the bay.",
    "A cobalt kite circles the mill while juniper clocks murmur softly.",
    "Obsidian ferries carry lavender maps toward forgotten coastal towns.",
]


def _metin_pdf(tmp_path: Path, satirlar, ad="kaynak.pdf", unicode_font=True) -> str:
    """Satırlar: her satır (metin, punto, taban_kayması[, x_geri]) parçaları.

    Parçalar yan yana dizilir; dördüncü öğe verilirse x o kadar geri alınır
    (üst üste binen glif: TeX'in birleşik uzun okları)."""
    doc = fitz.open()
    page = doc.new_page(width=595, height=842)
    if unicode_font and UNICODE_FONT.is_file():
        page.insert_font(fontname="uni", fontfile=str(UNICODE_FONT))
        font, fname = fitz.Font(fontfile=str(UNICODE_FONT)), "uni"
    else:
        font, fname = fitz.Font("helv"), "helv"
    y = 72.0
    for satir in satirlar:
        x = 56.0
        for parca in satir:
            metin, boy, dy = parca[0], parca[1], parca[2]
            if len(parca) > 3:
                x -= parca[3]
            page.insert_text((x, y + dy), metin, fontname=fname, fontsize=boy)
            x += font.text_length(metin, fontsize=boy)
        y += 26.0
    for d in DOLGU:
        page.insert_text((56.0, y), d, fontname=fname, fontsize=11)
        y += 16.0
    yol = tmp_path / ad
    doc.save(str(yol))
    doc.close()
    return str(yol)


def _gerekli_unicode_font():
    if not UNICODE_FONT.is_file():
        pytest.skip("Arial Unicode yok")


# --- Kalem 4: MT-46 ok simgesi ------------------------------------------------

ONCE_DENKLEM = "Then the identity below follows from the rule for products."
SONRA_DENKLEM = "Equation one matters for every orbit we study in this chapter."


def test_display_matematikte_ok_simgesi_pdfteki_gibi_olur(tmp_path):
    _gerekli_unicode_font()
    pdf = _metin_pdf(tmp_path, [[(ONCE_DENKLEM, 11, 0)],
                                [("x · y = ab ⇐ b = cd", 11, 0)],
                                [(SONRA_DENKLEM, 11, 0)]])
    md = (f"{ONCE_DENKLEM}\n\n$$x \\cdot y = ab \\iff b = cd$$\n\n{SONRA_DENKLEM}\n\n"
          + "\n\n".join(DOLGU) + "\n")
    yeni, rapor = pmo.matematik_tablo_onar_raporlu(md, pdf)
    assert "$$x \\cdot y = ab \\Leftarrow b = cd$$" in yeni, rapor.atlanan
    assert "denklem_ok" in rapor.sayim()


def test_birlesik_uzun_ok_degismez(tmp_path):
    # TeX'in \iff'i metin katmanında üst üste binen ⇐ ve ⇒: ⟺ doğrudur.
    _gerekli_unicode_font()
    ok_genislik = fitz.Font(fontfile=str(UNICODE_FONT)).text_length("⇐", fontsize=11)
    pdf = _metin_pdf(tmp_path, [[(ONCE_DENKLEM, 11, 0)],
                                [("x · y = ab ⇐", 11, 0), ("⇒ b = cd", 11, 0, 0.45 * ok_genislik)],
                                [(SONRA_DENKLEM, 11, 0)]])
    md = (f"{ONCE_DENKLEM}\n\n$$x \\cdot y = ab \\iff b = cd$$\n\n{SONRA_DENKLEM}\n\n"
          + "\n\n".join(DOLGU) + "\n")
    yeni, _ = pmo.matematik_tablo_onar_raporlu(md, pdf)
    assert "\\iff" in yeni


def test_ok_sayisi_farkliysa_dokunulmaz(tmp_path):
    _gerekli_unicode_font()
    pdf = _metin_pdf(tmp_path, [[(ONCE_DENKLEM, 11, 0)],
                                [("x · y = ab ⇐ b = cd", 11, 0)],
                                [(SONRA_DENKLEM, 11, 0)]])
    md = (f"{ONCE_DENKLEM}\n\n$$x \\cdot y = ab \\iff b \\to cd$$\n\n{SONRA_DENKLEM}\n\n"
          + "\n\n".join(DOLGU) + "\n")
    yeni, _ = pmo.matematik_tablo_onar_raporlu(md, pdf)
    assert "\\iff" in yeni and "\\to" in yeni


# --- Kalem 3: MT-14 ondalık virgül -----------------------------------------------

def test_matematikte_ondalik_virgul_pdfteki_gibi_olur(tmp_path):
    _gerekli_unicode_font()
    pdf = _metin_pdf(tmp_path, [[("A clay wall with s = 0,24 m and λ = 0,8 W/(m K) keeps the cellar cool.", 11, 0)]])
    md = ("A clay wall with  $s=0.24\\,\\mathrm{m}$  and  $\\lambda=0.8\\,\\mathrm{W/(m\\,K)}$  "
          "keeps the cellar cool.\n\n" + "\n\n".join(DOLGU) + "\n")
    yeni, rapor = pmo.matematik_tablo_onar_raporlu(md, pdf)
    assert "$s=0,24\\,\\mathrm{m}$" in yeni, rapor.atlanan
    assert "$\\lambda=0,8\\,\\mathrm{W/(m\\,K)}$" in yeni
    assert rapor.sayim().get("ondalik_virgul") == 2


def test_pdfte_nokta_ise_dokunulmaz(tmp_path):
    _gerekli_unicode_font()
    pdf = _metin_pdf(tmp_path, [[("A clay wall with s = 0.24 m keeps the cellar cool all summer.", 11, 0)]])
    md = ("A clay wall with  $s=0.24\\,\\mathrm{m}$  keeps the cellar cool all summer.\n\n"
          + "\n\n".join(DOLGU) + "\n")
    yeni, _ = pmo.matematik_tablo_onar_raporlu(md, pdf)
    assert "$s=0.24\\,\\mathrm{m}$" in yeni


def test_rakamlar_farkliysa_dokunulmaz(tmp_path):
    _gerekli_unicode_font()
    pdf = _metin_pdf(tmp_path, [[("A clay wall with s = 0,25 m keeps the cellar cool all summer.", 11, 0)]])
    md = ("A clay wall with  $s=0.24\\,\\mathrm{m}$  keeps the cellar cool all summer.\n\n"
          + "\n\n".join(DOLGU) + "\n")
    yeni, _ = pmo.matematik_tablo_onar_raporlu(md, pdf)
    assert "$s=0.24\\,\\mathrm{m}$" in yeni


# --- Kalem 5: RAPOR MT-8 matematik italik harfi --------------------------------

def _pdf_dizgisi(metin: str) -> str:
    return metin.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def _cmmi_pdf(tmp_path: Path, parcalar, ad="kaynak.pdf") -> str:
    """Tek satır: (metin, "helv" | "heit" | "CMMI10") parçaları yan yana.

    Bütün metin TEK içerik akışında, görsel sırayla yazılır (TeX çıktısındaki
    gibi). CMMI10 gömülmeyen bir Type1 yazı tipidir (TeX'in matematik
    italiği; metin katmanında yazı tipi adı "CMMI10" görünür)."""
    doc = fitz.open()
    page = doc.new_page(width=595, height=842)
    olcu = {"helv": fitz.Font("helv"), "heit": fitz.Font("heit")}
    adlar = {"helv": "Helvetica", "heit": "Helvetica-Oblique", "CMMI10": "CMMI10",
             "nokta": "Helvetica"}
    kaynak = {}
    for k, (anahtar, taban) in enumerate(adlar.items()):
        fx = doc.get_new_xref()
        # "nokta": StandardEncoding'li Helvetica; \307 = dotaccent (˙).
        kodlama = "" if anahtar == "nokta" else "/Encoding/WinAnsiEncoding"
        doc.update_object(fx, f"<</Type/Font/Subtype/Type1/BaseFont/{taban}{kodlama}>>")
        kaynak[anahtar] = (f"F{k}", fx)
    H = page.rect.height
    akis = []

    def yaz(x, y, metin, font, boy=11):
        dizgi = "\\307" if font == "nokta" else _pdf_dizgisi(metin)
        akis.append(f"BT /{kaynak[font][0]} {boy} Tf 1 0 0 1 {x:.2f} {H - y:.2f} Tm "
                    f"({dizgi}) Tj ET")

    x, y = 56.0, 100.0
    for metin, font in parcalar:
        if font == "alt":
            # Alt simge: 8 pt, taban çizgisi 2,5 pt aşağıda.
            yaz(x, y + 2.5, metin, "helv", boy=8)
            x += olcu["helv"].text_length(metin, fontsize=8)
            continue
        if font == "nokta":
            # Önceki harfin (6 pt) üstüne: harfle yatayda örtüşen aksan.
            yaz(x - 4.5, y - 1.0, metin, font)
            continue
        yaz(x, y, metin, font)
        x += 6.0 * len(metin) if font == "CMMI10" else olcu[font].text_length(metin, fontsize=11)
    yy = y + 30
    for d in DOLGU:
        yaz(56.0, yy, d, "helv")
        yy += 16
    sozluk = " ".join(f"/{ad} {fx} 0 R" for ad, fx in kaynak.values())
    doc.xref_set_key(page.xref, "Resources", f"<</Font <<{sozluk}>>>>")
    cx = doc.get_new_xref()
    doc.update_object(cx, "<<>>")
    doc.update_stream(cx, "\n".join(akis).encode("latin-1"))
    doc.xref_set_key(page.xref, "Contents", f"{cx} 0 R")
    yol = tmp_path / ad
    doc.save(str(yol))
    doc.close()
    return str(yol)


def test_matematik_italigi_matematige_doner(tmp_path):
    pdf = _cmmi_pdf(tmp_path, [
        ("During a short interval d", "helv"), ("t", "CMMI10"),
        (" the old engine ejects mass d", "helv"), ("m", "CMMI10"),
        (" with speed ", "helv"), ("w", "CMMI10"), (" relative to the hull.", "helv")])
    md = ("During a short interval d*t* the old engine ejects mass d*m* with speed *w* "
          "relative to the hull.\n\n" + "\n\n".join(DOLGU) + "\n")
    yeni, rapor = pmo.matematik_tablo_onar_raporlu(md, pdf)
    # Tek başına italik harf ("*w*") \\textit olarak doğru basılır; G1c'nin
    # kanıt testi bu biçimi sabitler: dokunulmaz.
    assert ("During a short interval $\\mathrm{d}t$ the old engine ejects mass "
            "$\\mathrm{d}m$ with speed *w* relative to the hull.") in yeni, (rapor.sayim(), rapor.atlanan)
    assert rapor.sayim().get("matematik_italik") == 2


def test_metin_italigi_matematige_donmez(tmp_path):
    pdf = _cmmi_pdf(tmp_path, [
        ("The keeper wrote ", "helv"), ("ink", "heit"),
        (" notes about every passing ship in the harbour.", "helv")])
    md = ("The keeper wrote *ink* notes about every passing ship in the harbour.\n\n"
          + "\n\n".join(DOLGU) + "\n")
    yeni, _ = pmo.matematik_tablo_onar_raporlu(md, pdf)
    assert "*ink*" in yeni and "$ink$" not in yeni


def test_noktali_harf_ve_ardindaki_alt_simge_tek_matematik(tmp_path):
    pdf = _cmmi_pdf(tmp_path, [
        ("The outer flow rate ", "helv"), ("m", "CMMI10"), ("˙", "nokta"), ("a", "alt"),
        (" stays below the cabin value at every altitude.", "helv")])
    md = ("The outer flow rate *m*˙<sub>a</sub> stays below the cabin value at every "
          "altitude.\n\n" + "\n\n".join(DOLGU) + "\n")
    # Alt simge aynı matematiğe katılmazsa dönüştürücü "$\\dot{m}$${}_{a}$"
    # üretirdi ("$$" display matematik açar).
    yeni, rapor = pmo.matematik_tablo_onar_raporlu(md, pdf)
    assert "$\\dot{m}_{a}$" in yeni, (rapor.sayim(), rapor.atlanan)


def test_kiril_alt_simge_metin_kipinde_kalir():
    # Matematik yazı tipinde Kiril glif yok: simgedeki Kiril harf \\text{} içinde
    # (kaynak TeX'te de p_{\\text{н}}); Latin ve Yunan harf matematikte kalır.
    assert pmo._metin_simgeleri("$\\dot{m}_{н}$") == "$\\dot{m}_{\\text{н}}$"
    assert pmo._metin_simgeleri("$p_{a}$") == "$p_{a}$"
    assert pmo._metin_simgeleri("$v_{α}^{′}$") == "$v_{α}^{′}$"


# --- Kalem 6: tablo hücresindeki düz rakam dipnot işareti ---------------------

def _tablo_pdf(tmp_path: Path) -> str:
    """İki satırlı tablo; dipnot işaretleri (küçük, yükseltilmiş) içerik
    akışında hücre satırlarından SONRA yazılır (bir rapordaki gibi)."""
    doc = fitz.open()
    page = doc.new_page(width=595, height=842)
    helv = fitz.Font("helv")
    s1a, s1b = "Access to the archive of the harbour, info", "about the harbour, or direct access"
    s2a, s2b = "Resetting the lantern (as part of the plan or because", "this keeper's lantern was dead)"
    page.insert_text((72, 100), s1a, fontsize=11)
    page.insert_text((72, 114), s1b, fontsize=11)
    page.insert_text((420, 100), "97", fontsize=11)
    page.insert_text((72, 140), s2a, fontsize=11)
    page.insert_text((72, 154), s2b, fontsize=11)
    page.insert_text((420, 140), "25", fontsize=11)
    # Üst simge işaretler en sonda.
    page.insert_text((72 + helv.text_length(s1a, fontsize=11), 96), "94", fontsize=7)
    page.insert_text((72 + helv.text_length(s2b, fontsize=11), 150), "95", fontsize=7)
    y = 200
    for d in DOLGU:
        page.insert_text((72, y), d, fontsize=11)
        y += 16
    yol = tmp_path / "kaynak.pdf"
    doc.save(str(yol))
    doc.close()
    return str(yol)


def _br(metin):
    return "<br>".join(metin.split())


def test_tablo_hucresindeki_dipnot_isareti_ust_simge_olur(tmp_path):
    pdf = _tablo_pdf(tmp_path)
    h1 = _br("Access to the archive of the harbour, info 94 about the harbour, or direct access")
    h2 = _br("95 Resetting the lantern (as part of the plan or because this keeper's lantern was dead)")
    md = ("| Category | Count |\n|---|---|\n"
          f"| {h1} | 97 |\n| {h2} | 25 |\n\n" + "\n\n".join(DOLGU) + "\n")
    yeni, rapor = pmo.matematik_tablo_onar_raporlu(md, pdf)
    assert "info<sup>94</sup><br>about" in yeni, (rapor.sayim(), rapor.atlanan)
    # Marker işareti hücre başına taşımıştı: PDF'teki yerine (sözcüğün ardına).
    assert "was<br>dead)<sup>95</sup>" in yeni
    assert "| 95<br>" not in yeni and "|95<br>" not in yeni
    assert rapor.sayim().get("tablo_dipnot") == 2


def test_tablo_hucresindeki_gercek_sayiya_dokunulmaz(tmp_path):
    pdf = _tablo_pdf(tmp_path)
    h1 = _br("Access to the archive of the harbour, info about the harbour, or direct access")
    md = ("| Category | Count |\n|---|---|\n"
          f"| {h1} | 97 |\n\n" + "\n\n".join(DOLGU) + "\n")
    yeni, _ = pmo.matematik_tablo_onar_raporlu(md, pdf)
    assert "| 97 |" in yeni and "<sup>97</sup>" not in yeni


def test_hucrede_yinelenen_sozcuk_komsu_sozcukle_ayrilir(tmp_path):
    # "lantern" hücrede iki kez geçiyor; PDF'teki "the lantern⁹⁵ (as" bağlamı
    # (önceki ve sonraki sözcük) doğru yeri seçer.
    doc = fitz.open()
    page = doc.new_page(width=595, height=842)
    helv = fitz.Font("helv")
    s1 = "Resetting the lantern"
    page.insert_text((72, 100), s1, fontsize=11)
    page.insert_text((72 + helv.text_length(s1, fontsize=11) + 8, 100),
                     "(as part of the plan or because", fontsize=11)
    page.insert_text((72, 114), "this keeper's lantern was dead)", fontsize=11)
    page.insert_text((420, 100), "25", fontsize=11)
    page.insert_text((72 + helv.text_length(s1, fontsize=11), 96), "95", fontsize=7)
    y = 200
    for d in DOLGU:
        page.insert_text((72, y), d, fontsize=11)
        y += 16
    pdf = str(tmp_path / "kaynak.pdf")
    doc.save(pdf)
    doc.close()
    h2 = _br("95 Resetting the lantern (as part of the plan or because this keeper's "
             "lantern was dead)")
    md = ("| Category | Count |\n|---|---|\n"
          f"| {h2} | 25 |\n\n" + "\n\n".join(DOLGU) + "\n")
    yeni, rapor = pmo.matematik_tablo_onar_raporlu(md, pdf)
    assert "Resetting<br>the<br>lantern<sup>95</sup><br>(as" in yeni, (rapor.sayim(), rapor.atlanan)


def test_mapsto_dikey_cizgi_ve_ok_birlesigidir(tmp_path):
    # TeX'in \mapsto'su metin katmanında sıfır genişlikli "|" + "→" (ölçüm:
    # derin öğrenme kitabı, MTSYN yazı tipi). Tek "→" sanılıp \rightarrow
    # yapılmamalı.
    _gerekli_unicode_font()
    pdf = _metin_pdf(tmp_path, [[(ONCE_DENKLEM, 11, 0)],
                                [("f : x |", 11, 0), ("→ y", 11, 0, 0.3)],
                                [(SONRA_DENKLEM, 11, 0)]])
    md = (f"{ONCE_DENKLEM}\n\n$$f : x \\mapsto y$$\n\n{SONRA_DENKLEM}\n\n"
          + "\n\n".join(DOLGU) + "\n")
    yeni, _ = pmo.matematik_tablo_onar_raporlu(md, pdf)
    assert "\\mapsto" in yeni and "\\rightarrow" not in yeni


# ---------------------------------------------------------------------------
# Boru hattına bağlama (translate_book.run_pipeline, dönüştürücüden önce)
# ---------------------------------------------------------------------------

def test_boru_hatti_iki_donusum_yerinde_de_onarir():
    import inspect
    import translate_book as tb
    kaynak = inspect.getsource(tb.run_pipeline)
    assert kaynak.count("converter.convert(_pdf_ile_markdown_onar(") == 2
    assert "converter.convert(markdown_text)" not in kaynak


def test_pdf_ile_markdown_onar_hepsini_uygular(tmp_path):
    import logging
    import translate_book as tb
    from latex_pipeline.config import PipelineConfig

    out = tmp_path / "kitap"
    (out / "source" / "images").mkdir(parents=True)
    # Kayıp başlıklı iki sayfalık PDF + kesik şekil: aynı PDF'te ikisi birden
    # gerekmez; iki ayrı dosyayla iki ayrı çağrı.
    pdf = _kayip_pdf(tmp_path, GOVDE, GOVDE2, baslik_sayfa1=BASLIK_KAYIP,
                     baslik_sayfa1_basa=BASLIK_A)
    (out / "source_book.pdf").write_bytes(Path(pdf).read_bytes())
    cfg = PipelineConfig(input_pdf=str(out / "source_book.pdf"), output_dir=str(out),
                         document_class="book")
    md = f"#### {BASLIK_A}\n\n{_paragraf(GOVDE)}\n\n{_paragraf(GOVDE2)}\n"
    yeni = tb._pdf_ile_markdown_onar(md, cfg, logging.getLogger("test"))
    assert f"#### {BASLIK_KAYIP}" in yeni

    # Şekil: kesik raster genişler (görsel dosyası değişir).
    out2 = tmp_path / "kitap2"
    out2.mkdir()
    pdf2, img_dir = _pdf_ve_kirpim(out2)
    (out2 / "source").mkdir(exist_ok=True)
    hedef = out2 / "source" / "images"
    Path(img_dir).rename(hedef)
    cfg2 = PipelineConfig(input_pdf=pdf2, output_dir=str(out2), document_class="book")
    once = (hedef / "_page_0_Figure_1.png").read_bytes()
    tb._pdf_ile_markdown_onar(_md("_page_0_Figure_1.jpeg"), cfg2, logging.getLogger("test"))
    assert (hedef / "_page_0_Figure_1.png").read_bytes() != once


def test_pdf_yoksa_markdown_aynen(tmp_path):
    import logging
    import translate_book as tb
    from latex_pipeline.config import PipelineConfig
    cfg = PipelineConfig(input_pdf=str(tmp_path / "source_book.pdf"), output_dir=str(tmp_path),
                         document_class="book")
    md = "EPUB metni: dokunulmaz.\n"
    assert tb._pdf_ile_markdown_onar(md, cfg, logging.getLogger("test")) is md


def test_paragrafin_ilk_satiri_sonraki_paragrafin_basina_girer(tmp_path):
    # İkinci sayfanın (ayrı blok) ilk satırı düşmüş: sonraki paragrafın
    # başına, açılan vurgu işaretinin dışına girer.
    pdf = _kayip_pdf(tmp_path, GOVDE, GOVDE2)
    md = (f"{_paragraf(GOVDE)}\n\n**{GOVDE2[1]}** {_paragraf(GOVDE2[2:])}\n")
    yeni, rapor = kk.onar_raporlu(md, pdf)
    assert len(rapor.eklemeler) == 1, (rapor.atlanan, rapor.eklenmeyen)
    assert f"\n\n{GOVDE2[0]} **{GOVDE2[1]}**" in yeni


def test_ikinci_kosuda_parca_yine_kaldirilir(tmp_path):
    # Markdown her dönüşümde full_book.md'den taze okunur: ikinci koşuda ana
    # görsel kayıttan tanınsa da yinelenen parça satırı yine çıkmalı.
    pdf, img_dir = _pdf_ve_kirpim(tmp_path, kirpim=(110, 240, 490, 420))
    logo_pt = (100 + 590 * 0.5, 200 + 436 * 0.5, 495, 200 + 484 * 0.5)
    _kirp_kaydet(pdf, 0, logo_pt, Path(img_dir) / "_page_0_Figure_2.png")
    md = _md("_page_0_Figure_1.jpeg", "_page_0_Figure_2.jpeg")
    ilk, _ = sk.onar_raporlu(md, pdf, img_dir)
    ikinci, rapor = sk.onar_raporlu(md, pdf, img_dir)
    assert ikinci == ilk
    assert "_page_0_Figure_2" not in ikinci
    assert rapor.atlanan.get("zaten_genisletilmis") == 1
    assert not rapor.bu_kosuda_genisletilen
