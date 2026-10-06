"""E kulvarı (6 Ekim 2026): kaynak PDF'teki çerçeveli / zeminli kutular
(mt okuma MT-24).

Marker kutuları düşürüyordu; çerçeve ya da renkli zemin içindeki paragraflar
Türkçede sıradan paragraf basılıyordu. ``latex_pipeline/kutu_onarim.py``
PDF çizimlerinden kutuyu bulup içine TAM düşen bütün Markdown bloklarını
işaret satırlarıyla sarar; dönüştürücü ``\\begin{kckutu}…\\end{kckutu}``
basar; şablon ortamı tanımlar; çevirmen, sanitizer ve QC ortamı tanır.

KARAR 26 gereği testlerde kitap metni YOK: bütün cümleler ve PDF'ler
sentetiktir (PyMuPDF ile üretilir; aynı çizim yapıları: framed'in parça
parça kenarları, tcolorbox'un iki katmanlı dolgusu, renkli tablo satırları).
Model yüklenmez; çevirmen MagicMock (yalnız test altyapısı).
"""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path
from unittest.mock import MagicMock

import fitz  # PyMuPDF
import pytest

from latex_pipeline import kutu_onarim as K
from latex_pipeline.config import PipelineConfig

_PUNTO = 10
_SIYAH = (0, 0, 0)


# ---------------------------------------------------------------------------
# Sentetik PDF yardımcıları
# ---------------------------------------------------------------------------

def _yaz(page, y, metin, x=72):
    page.insert_text((x, y), metin, fontname="helv", fontsize=_PUNTO)


def _framed_cerceve(page, r, kalinlik=0.5):
    """LaTeX framed gibi: üst/alt kenar + parça parça sol/sağ kenar (ince
    dolu dikdörtgenler)."""
    x0, y0, x1, y1 = r
    orta = (y0 + y1) / 2
    parcalar = [(x0, y0, x1, y0 + kalinlik), (x0, y1 - kalinlik, x1, y1)]
    for a, b in ((y0, orta + 1), (orta - 1, y1)):
        parcalar += [(x0, a, x0 + kalinlik, b), (x1 - kalinlik, a, x1, b)]
    for p in parcalar:
        page.draw_rect(fitz.Rect(*p), color=None, fill=_SIYAH)


def _zeminli_kutu(page, r):
    """tcolorbox gibi: koyu dış dolgu (çerçeve) + 0,6 pt içeride açık zemin."""
    x0, y0, x1, y1 = r
    page.draw_rect(fitz.Rect(x0, y0, x1, y1), color=None, fill=(0.45, 0.45, 0.45))
    page.draw_rect(fitz.Rect(x0 + 0.6, y0 + 0.6, x1 - 0.6, y1 - 0.6),
                   color=None, fill=(0.95, 0.95, 0.95))


def _kaydet(doc, tmp_path, ad="kaynak.pdf") -> str:
    yol = tmp_path / ad
    doc.save(str(yol))
    return str(yol)


_GIRIS = "The pump station was built near the river in the spring."
_KURAL1 = "First rule: the valve stays closed while the pump runs."
_KURAL2 = "Second rule: the tank level is checked every hour."
_SONRA = "After the box the report describes the pipes and the filters."
_MD_KURAL = (f"# Pump Station Notes\n\n{_GIRIS}\n\n{_KURAL1}\n\n{_KURAL2}\n\n"
             f"{_SONRA}\n")


def _kural_pdf(tmp_path, cizim="framed") -> str:
    doc = fitz.open()
    page = doc.new_page()
    _yaz(page, 80, "Pump Station Notes")
    _yaz(page, 110, _GIRIS)
    if cizim == "framed":
        _framed_cerceve(page, (66, 125, 530, 175))
    else:
        _zeminli_kutu(page, (66, 125, 530, 175))
    _yaz(page, 142, _KURAL1)
    _yaz(page, 162, _KURAL2)
    _yaz(page, 200, _SONRA)
    return _kaydet(doc, tmp_path)


def _kutu_satirlari(md: str):
    """İşaretli Markdown'dan (kutu içi dolu satırlar) listesi."""
    out, cur, ic = [], None, False
    for s in md.split("\n"):
        if s.strip() == K.KUTU_BAS:
            cur, ic = [], True
        elif s.strip() == K.KUTU_SON:
            out.append(cur)
            ic = False
        elif ic and s.strip():
            cur.append(s)
    return out


# ---------------------------------------------------------------------------
# 1. Algılama: gerçek kutular
# ---------------------------------------------------------------------------

class TestKutuAlgilama:
    def test_framed_cerceve_iki_paragrafi_sarar(self, tmp_path):
        md, rap = K.onar_raporlu(_MD_KURAL, _kural_pdf(tmp_path))
        assert _kutu_satirlari(md) == [[_KURAL1, _KURAL2]], md
        assert rap.sayac.get("kutu") == 1

    def test_zeminli_kutu_sarar(self, tmp_path):
        md, _ = K.onar_raporlu(_MD_KURAL, _kural_pdf(tmp_path, "zemin"))
        assert _kutu_satirlari(md) == [[_KURAL1, _KURAL2]]

    def test_kutu_etiketi_ve_basligi_kutuyla_birlikte(self, tmp_path):
        """Kutu etiketi ("Note 1.1") ve başlık Marker'da başlık satırı;
        kutunun içindeyse kutuya girer."""
        doc = fitz.open()
        page = doc.new_page()
        _yaz(page, 100, _GIRIS)
        _zeminli_kutu(page, (62, 120, 532, 210))
        _yaz(page, 138, "Note 1.1", x=72)
        _yaz(page, 156, "Contact resistance between two plates", x=72)
        body = ("Two metal plates touch only at a few points because the "
                "surfaces are rough.")
        _yaz(page, 180, body, x=72)
        _yaz(page, 240, _SONRA)
        pdf = _kaydet(doc, tmp_path)
        md = (f"{_GIRIS}\n\n#### **Note 1.1**\n\n"
              f"#### **Contact resistance between two plates**\n\n{body}\n\n"
              f"{_SONRA}\n")
        out, _ = K.onar_raporlu(md, pdf)
        assert _kutu_satirlari(out) == [[
            "#### **Note 1.1**", "#### **Contact resistance between two plates**",
            body]]

    def test_sayfa_sinirinda_bolunen_kutu_tek_kutu_olur(self, tmp_path):
        """Sayfa 1'in altındaki parça cümle ortasında biter, sayfa 2'nin
        üstündeki parça devam eder; arada "(continued)" artığı."""
        doc = fitz.open()
        p1 = doc.new_page()
        _yaz(p1, 600, _GIRIS)
        _zeminli_kutu(p1, (62, 620, 532, 700))
        bas = "The engineers wrote a long answer about the pumps and the"
        bas2 = "valves that keep the pressure stable in the long pipes and the"
        _yaz(p1, 640, "The engineers answered every question about the station.")
        _yaz(p1, 670, bas)
        _yaz(p1, 684, bas2)
        _yaz(p1, 720, "(continued)", x=480)
        p2 = doc.new_page()
        _zeminli_kutu(p2, (62, 60, 532, 120))
        devam = "small tanks that store water for the dry summer months."
        _yaz(p2, 80, devam)
        _yaz(p2, 104, "The answer ended with a short list of spare parts.")
        _yaz(p2, 150, _SONRA)
        pdf = _kaydet(doc, tmp_path)
        md = (f"{_GIRIS}\n\nThe engineers answered every question about the station.\n\n"
              f"{bas} {bas2}\n\n(continued)\n\n{devam}\n\n"
              "The answer ended with a short list of spare parts.\n\n"
              f"{_SONRA}\n")
        out, rap = K.onar_raporlu(md, pdf)
        kutular = _kutu_satirlari(out)
        assert len(kutular) == 1, out
        assert kutular[0][0].startswith("The engineers answered")
        assert kutular[0][-1].startswith("The answer ended")
        assert rap.sayac.get("zincir") == 1

    def test_devam_etiketiyle_bolunen_kutu_paragraf_sinirinda(self, tmp_path):
        """Kutu sayfa sınırında paragraf arasından bölünmüş; aradaki
        "(continued)" etiketi kutunun sürdüğünü gösterir: tek kutu."""
        doc = fitz.open()
        p1 = doc.new_page()
        _yaz(p1, 600, _GIRIS)
        _zeminli_kutu(p1, (62, 620, 532, 680))
        bir = "The first answer paragraph explains how the valves are tested."
        _yaz(p1, 645, bir)
        _yaz(p1, 700, "(continued)", x=480)
        p2 = doc.new_page()
        _zeminli_kutu(p2, (62, 60, 532, 100))
        iki = "Furthermore, the second paragraph lists the spare parts we keep."
        _yaz(p2, 85, iki)
        _yaz(p2, 140, _SONRA)
        pdf = _kaydet(doc, tmp_path)
        md = f"{_GIRIS}\n\n{bir}\n\n(continued)\n\n{iki}\n\n{_SONRA}\n"
        out, rap = K.onar_raporlu(md, pdf)
        assert _kutu_satirlari(out) == [[bir, "(continued)", iki]], out
        assert rap.sayac.get("zincir") == 1

    def test_ardisik_sayfada_iki_ayri_tam_kutu_birlesmez(self, tmp_path):
        doc = fitz.open()
        p1 = doc.new_page()
        _zeminli_kutu(p1, (62, 620, 532, 680))
        bir = "The first box tells the history of the station in one sentence."
        _yaz(p1, 645, bir)
        p2 = doc.new_page()
        _zeminli_kutu(p2, (62, 60, 532, 100))
        iki = "Another box gives the opening hours of the visitor centre."
        _yaz(p2, 85, iki)
        pdf = _kaydet(doc, tmp_path)
        md = f"{_GIRIS}\n\n{bir}\n\n{iki}\n\n{_SONRA}\n"
        out, rap = K.onar_raporlu(md, pdf)
        assert _kutu_satirlari(out) == [[bir], [iki]], out
        assert not rap.sayac.get("zincir")


# ---------------------------------------------------------------------------
# 2. Algılama: kutu OLMAYANLAR (temkin)
# ---------------------------------------------------------------------------

class TestKutuDegil:
    def test_renkli_tablo_satirlari(self, tmp_path):
        doc = fitz.open()
        page = doc.new_page()
        satirlar = ["Valve type one has a long name and a short code.",
                    "Valve type two has a longer name and another code.",
                    "Valve type three is used only in the winter months."]
        for k, s in enumerate(satirlar):
            y0 = 120 + 30 * k
            page.draw_rect(fitz.Rect(66, y0, 530, y0 + 30), color=None,
                           fill=(0.85, 0.9, 0.95) if k % 2 else (0.95, 0.95, 0.85))
            _yaz(page, y0 + 19, s)
        pdf = _kaydet(doc, tmp_path)
        md = "\n\n".join(satirlar) + "\n"
        out, rap = K.onar_raporlu(md, pdf)
        assert out == md
        assert rap.sayac.get("ret_bitisik", 0) >= 2

    def test_gorselli_sekil_cercevesi(self, tmp_path):
        doc = fitz.open()
        page = doc.new_page()
        _framed_cerceve(page, (66, 100, 530, 330))
        pix = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 40, 40), False)
        pix.set_rect(pix.irect, (120, 160, 200))
        page.insert_image(fitz.Rect(100, 110, 496, 290), pixmap=pix)
        cap = "Figure 1.1: The pump house seen from the river bank at noon."
        _yaz(page, 310, cap)
        pdf = _kaydet(doc, tmp_path)
        md = f"![](_page_0_Picture_1.jpeg)\n\n{cap}\n"
        out, rap = K.onar_raporlu(md, pdf)
        assert out == md
        assert rap.sayac.get("ret_gorsel") == 1

    def test_sayfa_kenarligi(self, tmp_path):
        doc = fitz.open()
        page = doc.new_page()
        w, h = page.rect.width, page.rect.height
        page.draw_rect(fitz.Rect(1, 1, w - 1, h - 1), color=_SIYAH, width=0.8)
        _yaz(page, 110, _GIRIS)
        _yaz(page, 130, _KURAL1)
        pdf = _kaydet(doc, tmp_path)
        md = f"{_GIRIS}\n\n{_KURAL1}\n"
        out, _ = K.onar_raporlu(md, pdf)
        assert out == md

    def test_yinelenen_ust_bilgi_cubugu(self, tmp_path):
        doc = fitz.open()
        parcalar = []
        for k in range(3):
            page = doc.new_page()
            _zeminli_kutu(page, (62, 30, 532, 70))
            metin = f"Station handbook part {k + 1} with notes for the night shift crew."
            _yaz(page, 54, metin)
            parcalar.append(metin)
        pdf = _kaydet(doc, tmp_path)
        md = "\n\n".join(parcalar) + "\n"
        out, rap = K.onar_raporlu(md, pdf)
        assert out == md
        assert rap.sayac.get("ret_yinelenen") == 3

    def test_paragraf_ortasindan_bolmez(self, tmp_path):
        """Kutu bir Markdown paragrafının yalnız bir kısmını sarıyor."""
        doc = fitz.open()
        page = doc.new_page()
        _zeminli_kutu(page, (62, 120, 532, 150))
        _yaz(page, 140, _KURAL1)
        _yaz(page, 170, _KURAL2)          # aynı paragrafın kutu dışı devamı
        pdf = _kaydet(doc, tmp_path)
        md = f"{_GIRIS}\n\n{_KURAL1} {_KURAL2}\n"
        out, rap = K.onar_raporlu(md, pdf)
        assert out == md
        assert rap.sayac.get("ret_blok_siniri_yok") == 1

    def test_cumle_ortasinda_biten_yarim_kutu(self, tmp_path):
        """Sayfa altındaki kutu cümle ortasında bitiyor, devamı kutu değil."""
        doc = fitz.open()
        page = doc.new_page()
        _zeminli_kutu(page, (62, 700, 532, 740))
        yarim = "The engineers wrote a long answer about the pumps and the"
        _yaz(page, 724, yarim)
        doc.new_page()
        _yaz(doc[1], 80, "valves in the old building near the gate.")
        pdf = _kaydet(doc, tmp_path)
        md = f"{_GIRIS}\n\n{yarim}\n\nvalves in the old building near the gate.\n"
        out, rap = K.onar_raporlu(md, pdf)
        assert out == md
        assert rap.sayac.get("ret_acik_uclu_bitiyor") == 1

    def test_tablo_blogu_kutuya_girmez(self, tmp_path):
        doc = fitz.open()
        page = doc.new_page()
        _framed_cerceve(page, (66, 100, 530, 180))
        _yaz(page, 120, "Valve Size Pressure")
        _yaz(page, 140, "Gate 40 mm 6 bar and more for the summer")
        _yaz(page, 160, "Ball 25 mm 10 bar and less for the winter")
        pdf = _kaydet(doc, tmp_path)
        md = ("| Valve | Size | Pressure |\n|---|---|---|\n"
              "| Gate | 40 mm | 6 bar and more for the summer |\n"
              "| Ball | 25 mm | 10 bar and less for the winter |\n")
        out, _ = K.onar_raporlu(md, pdf)
        assert out == md


# ---------------------------------------------------------------------------
# 3. Kayıpsızlık ve işaret sözleşmesi
# ---------------------------------------------------------------------------

class TestIsaretSozlesmesi:
    def test_ayiklaninca_markdown_birebir_ayni(self, tmp_path):
        md, _ = K.onar_raporlu(_MD_KURAL, _kural_pdf(tmp_path))
        assert md != _MD_KURAL
        satirlar, olaylar = K.isaretleri_ayikla(md.split("\n"))
        assert satirlar == _MD_KURAL.split("\n")
        bas = [i for i, t in olaylar if t == "bas"]
        son = [i for i, t in olaylar if t == "son"]
        assert satirlar[bas[0]] == _KURAL1
        assert satirlar[son[0] - 1] == _KURAL2 and not satirlar[son[0]].strip()

    def test_bitisik_kutular_once_son_sonra_bas(self):
        md = f"A text.\n\n{K.KUTU_BAS}\n\nB one.\n\n{K.KUTU_SON}\n\n{K.KUTU_BAS}\n\nC two.\n\n{K.KUTU_SON}\n"
        satirlar, olaylar = K.isaretleri_ayikla(md.split("\n"))
        assert satirlar == ["A text.", "", "B one.", "", "C two.", ""]
        assert olaylar == [(2, "bas"), (3, "son"), (4, "bas"), (5, "son")]

    def test_pdf_yoksa_aynen(self, tmp_path):
        assert K.onar_raporlu(_MD_KURAL, None)[0] == _MD_KURAL
        assert K.onar_raporlu(_MD_KURAL, str(tmp_path / "yok.pdf"))[0] == _MD_KURAL
        assert K.onar_raporlu(_MD_KURAL, str(tmp_path / "k.epub"))[0] == _MD_KURAL

    def test_zaten_isaretliyse_dokunmaz(self, tmp_path):
        md = f"{K.KUTU_BAS}\n\n{_KURAL1}\n"
        out, rap = K.onar_raporlu(md, _kural_pdf(tmp_path))
        assert out == md and rap.sayac.get("zaten_isaretli") == 1

    def test_kod_citi_ici_bos_satir_blok_siniri_degil(self):
        md = "Intro text here.\n\n```\nline one\n\nline two\n```\n\nAfter.\n"
        bl = K.bloklar(md.split("\n"))
        assert [(b.bas, b.son, b.tur) for b in bl] == [
            (0, 0, "paragraf"), (2, 6, "kod"), (8, 8, "paragraf")]


# ---------------------------------------------------------------------------
# 4. Dönüştürücü
# ---------------------------------------------------------------------------

def _donustur(md, pdf, tmp_path, kutu=True, monkeypatch=None):
    import latex_pipeline.converter as C
    if not kutu:
        monkeypatch.setattr(C.kutu_onarim, "onar_raporlu",
                            lambda m, p: (m, K.KutuRaporu()))
    conv = C.MarkdownToLatex(PipelineConfig(document_class="book",
                                            output_dir=str(tmp_path),
                                            input_pdf=pdf))
    return conv.convert(md), conv


class TestDonusturucu:
    def test_kutu_ortamla_basilir_metin_aynen(self, tmp_path, monkeypatch):
        pdf = _kural_pdf(tmp_path)
        acik, conv = _donustur(_MD_KURAL, pdf, tmp_path)
        assert conv.kutu_sayaci == {"basildi": 1}
        satir = acik.split("\n")
        b = satir.index("\\begin{kckutu}")
        e = satir.index("\\end{kckutu}")
        ic = [s for s in satir[b + 1:e] if s.strip()]
        assert len(ic) == 2 and ic[0].endswith("the pump runs.") \
            and ic[1].endswith("every hour.")
        kapali, _ = _donustur(_MD_KURAL, pdf, tmp_path, kutu=False,
                              monkeypatch=monkeypatch)
        temiz = "\n".join(s for s in satir
                          if s not in ("\\begin{kckutu}", "\\end{kckutu}"))
        assert temiz == kapali

    def _hazir(self, tmp_path, satirlar, olaylar):
        import latex_pipeline.converter as C
        conv = C.MarkdownToLatex(PipelineConfig(document_class="book",
                                                output_dir=str(tmp_path)))
        conv._kutu_olaylari = list(olaylar)
        conv._kutu_acik = None
        conv.kutu_sayaci = {}
        return conv

    @pytest.mark.parametrize("icerik,neden", [
        ("\\section{Valves}", "yasak_icerik"),
        ("\\chapter{Pumps}", "yasak_icerik"),
        ("\\begin{figure}[H]", "yasak_icerik"),
        ("Text with a note\x02F0\x02 inside.", "yasak_icerik"),
        ("\\begin{longtable}{ll}", "yasak_icerik"),
        ("", "bos"),
    ])
    def test_kutuya_uygun_olmayan_icerik_kutusuz_kalir(self, tmp_path, icerik, neden):
        conv = self._hazir(tmp_path, [], [(0, "bas"), (1, "son")])
        out = ["onceki"]
        conv._kutu_olaylarini_isle(["x", ""], 0, out)
        out.append(icerik)
        conv._kutu_olaylarini_isle(["x", ""], 1, out)
        assert out == ["onceki", icerik]
        assert conv.kutu_sayaci == {neden: 1}

    def test_alt_bolum_basligi_kutuda_kalabilir(self, tmp_path):
        conv = self._hazir(tmp_path, [], [(0, "bas"), (1, "son")])
        out = []
        conv._kutu_olaylarini_isle(["x", ""], 0, out)
        out.append("\\subsubsection*{Note 1.1 — Contact resistance}")
        conv._kutu_olaylarini_isle(["x", ""], 1, out)
        assert out[0] == "\\begin{kckutu}" and out[-1] == "\\end{kckutu}"

    def test_tuketici_dolu_satir_yutarsa_kutu_geri_alinir(self, tmp_path):
        """Liste tüketicisi kutu sonunu ve kutu dışındaki dolu satırı yuttu."""
        satirlar = ["- a", "- b", "", "outside text", ""]
        conv = self._hazir(tmp_path, satirlar, [(0, "bas"), (2, "son")])
        out = []
        conv._kutu_olaylarini_isle(satirlar, 0, out)
        out.append("\\begin{itemize}\\item a\\item b\\item outside\\end{itemize}")
        conv._kutu_olaylarini_isle(satirlar, 4, out)
        assert "\\begin{kckutu}" not in out and "\\end{kckutu}" not in out
        assert conv.kutu_sayaci == {"son_yutuldu": 1}

    def test_tuketici_yalniz_bos_satir_yutarsa_kutu_kapanir(self, tmp_path):
        satirlar = ["- a", "- b", "", "", "after"]
        conv = self._hazir(tmp_path, satirlar, [(0, "bas"), (2, "son")])
        out = []
        conv._kutu_olaylarini_isle(satirlar, 0, out)
        out.append("\\begin{itemize}\\item a\\item b\\end{itemize}")
        conv._kutu_olaylarini_isle(satirlar, 4, out)
        assert out[-1] == "\\end{kckutu}"


# ---------------------------------------------------------------------------
# 5. Yerel model çevirmeni: ortam satırı modele gitmez
# ---------------------------------------------------------------------------

_KUTULU_GOVDE = (
    "The pump moves water from the well to the tank every morning.\n\n"
    "\\begin{kckutu}\n"
    "\\textbf{First rule:} The valve must stay closed while the pump is running.\n\n"
    "\\textbf{Second rule:} The tank is checked before the night shift begins.\n"
    "\\end{kckutu}\n\n"
    "After the box the text continues with a normal paragraph about pipes.\n")


class TestCevirmen:
    def _lt(self, calls):
        from latex_pipeline.translator import LatexTranslator
        lt = LatexTranslator(
            pipeline_config=PipelineConfig(translate=True, segment_timeout=60),
            mt_engine="hymt-7b", source_lang="en", target_lang="tr")

        def cevir(t):
            return "Çeviri: " + t

        bt = MagicMock()
        bt.translate_batch_protected = lambda texts: (calls.extend(texts),
                                                      [cevir(t) for t in texts])[1]
        bt.translate_text = lambda t, *a, **k: (calls.append(t), cevir(t))[1]
        lt._translator = bt
        return lt

    def test_ortam_yer_tutucuya_alinir(self):
        lt = self._lt([])
        body, stash = lt._stash_protected(_KUTULU_GOVDE)
        assert "kckutu" not in body
        assert "\\begin{kckutu}" in stash and "\\end{kckutu}" in stash

    def test_ortam_satiri_modele_gitmez_ve_aynen_kalir(self):
        calls = []
        out = self._lt(calls).translate(_KUTULU_GOVDE)
        assert calls, "modele hiç istek gitmedi"
        assert not any("kckutu" in c for c in calls), calls
        satir = out.split("\n")
        assert satir.count("\\begin{kckutu}") == 1
        assert satir.count("\\end{kckutu}") == 1


# ---------------------------------------------------------------------------
# 6. Sanitizer: eşi olmayan ortam satırı (derleme bozulmaz)
# ---------------------------------------------------------------------------

def _tam_belge(govde: str) -> str:
    return ("\\documentclass{book}\n\\begin{document}\n" + govde
            + "\n\\end{document}\n")


class TestSanitizer:
    def _san(self, govde):
        from latex_pipeline.sanitizer import LatexSanitizer
        out, rap = LatexSanitizer().sanitize(_tam_belge(govde))
        return out, rap

    def test_dengeli_kutu_aynen(self):
        out, _ = self._san("Before.\n\n\\begin{kckutu}\nInside text.\n\\end{kckutu}\n\nAfter.")
        assert out.count("\\begin{kckutu}") == 1 and out.count("\\end{kckutu}") == 1

    @pytest.mark.parametrize("govde", [
        "Before.\n\n\\begin{kckutu}\nInside text.\n\nAfter.",          # son düştü
        "Before.\n\nInside text.\n\\end{kckutu}\n\nAfter.",            # baş düştü
        "Before.\n\\end{kckutu}\nInside text.\n\\begin{kckutu}\nAfter.",  # sıra bozuk
    ])
    def test_esi_olmayan_satir_kalkar_metin_kalir(self, govde):
        out, rap = self._san(govde)
        assert "kckutu" not in out
        for parca in ("Before.", "Inside text.", "After."):
            assert parca in out
        assert any("Unmatched box environment" in d for d in rap.details)

    def test_kod_ornegindeki_ortam_sayilmaz(self):
        govde = ("\\begin{verbatim}\n\\begin{kckutu}\n\\end{verbatim}\n\n"
                 "\\begin{kckutu}\nInside.\n\\end{kckutu}")
        out, _ = self._san(govde)
        assert out.count("\\begin{kckutu}") == 2 and out.count("\\end{kckutu}") == 1


# ---------------------------------------------------------------------------
# 7. QC ve EPUB
# ---------------------------------------------------------------------------

class TestQcEpub:
    def test_qc_kutudaki_ozel_adlari_gorur(self):
        from latex_pipeline.qc_engine import QCEngine
        metin = ("Intro line.\n\n\\begin{kckutu}\nThe pumps were paid by the "
                 "Bank of England twice.\n\\end{kckutu}\n")
        adlar = QCEngine()._extract_proper_nouns(metin)
        assert "Bank of England" in adlar, adlar

    def test_epub_birimleri_ortam_satirini_yok_sayar(self):
        from latex_pipeline.epub_output import _tex_units
        kutulu = "Intro line.\n\n\\begin{kckutu}\nInside text.\n\\end{kckutu}\n\nAfter."
        kutusuz = "Intro line.\n\nInside text.\n\nAfter."
        assert [u.text for u in _tex_units(kutulu)] == \
            [u.text for u in _tex_units(kutusuz)]


# ---------------------------------------------------------------------------
# 8. Şablon: ortam tanımlı, yeni paket yok, derlenir
# ---------------------------------------------------------------------------

_SABLON = Path(__file__).resolve().parent.parent / "latex_pipeline" / "templates" / "turkish_book.tex"


def _kutu_tanimi() -> str:
    s = _SABLON.read_text(encoding="utf-8")
    a = s.index("%% --- Çerçeveli kutu")
    return s[a:s.index("%% ---", a + 10)]


class TestSablon:
    def test_ortam_tanimli_paket_eklenmez(self):
        tanim = _kutu_tanimi()
        assert "\\newenvironment{" + K.ORTAM + "}" in tanim
        assert "\\usepackage" not in tanim and "\\RequirePackage" not in tanim
        s = _SABLON.read_text(encoding="utf-8")
        assert s.index("\\usepackage[dvipsnames]{xcolor}") < s.index(tanim)

    @pytest.mark.skipif(shutil.which("xelatex") is None or shutil.which("pdftotext") is None,
                        reason="xelatex/pdftotext yok")
    def test_kisa_ve_uzun_kutu_derlenir_metin_eksiksiz(self, tmp_path):
        from latex_pipeline.template_engine import TemplateEngine
        cumle = "Bu kutu cümlesi eksiksiz basılmalı, hiçbir satırı kesilmemeli. "
        uzun = "\n\n".join(f"Uzun kutunun {k}. paragrafı. " + cumle * 6
                           for k in range(1, 15))
        govde = ("\\chapter{Deneme}\n\nKutudan önceki paragraf.\n\n"
                 "\\begin{kckutu}\n\\subsubsection*{Ara Not 1.1 — Kısa kutu}\n\n"
                 "Kısa kutunun tek paragrafı.\n\\begin{itemize}\n\\item birinci madde\n"
                 "\\end{itemize}\n\\end{kckutu}\n\nArada düz paragraf.\n\n"
                 "\\begin{kckutu}\n" + uzun + "\n\\end{kckutu}\n\nUzun kutudan sonra.\n")
        tex = TemplateEngine(PipelineConfig(document_class="book")).wrap_content(govde)
        (tmp_path / "k.tex").write_text(tex, encoding="utf-8")
        subprocess.run(["xelatex", "-interaction=nonstopmode", "-halt-on-error", "k.tex"],
                       cwd=tmp_path, capture_output=True, timeout=300)
        log = (tmp_path / "k.log").read_text(encoding="utf-8", errors="replace")
        assert not [l for l in log.splitlines() if l.startswith("! ")]
        assert "Overfull \\vbox" not in log
        metin = subprocess.run(["pdftotext", str(tmp_path / "k.pdf"), "-"],
                               capture_output=True, text=True, timeout=60).stdout
        metin = re.sub(r"\s+", " ", metin)
        for k in range(1, 15):
            assert f"Uzun kutunun {k}. paragrafı." in metin
        for parca in ("Kısa kutunun tek paragrafı.", "Uzun kutudan sonra.",
                      "Ara Not 1.1"):
            assert parca in metin
