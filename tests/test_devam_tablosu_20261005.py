"""KARAR 30 (5 Ekim 2026): başlığı yinelenerek "devamı" etiketiyle süren tablo.

Sayfa sonunda bölünüp sonraki sayfada başlık satırı yinelenerek ve devam
etiketiyle ("(continued)", "(Fortsetzung)", "(продолжение)" …) süren tablo
tek tabloda birleşir; yinelenen başlık ve etiketler sayfalama artığıdır ve
sayılarak silinir. Gerçek örnek bir test kitabında iki tablo (bkz. KARAR 30);
buradaki metinler sentetiktir.
"""
import re
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from latex_pipeline import marker_onarim as mo  # noqa: E402

_BASLIK = "| Yöntem adı | Ne sağlar |\n|---|---|\n"
_A = _BASLIK + "| Birinci yöntem | Kısa açıklama bir |\n| İkinci yöntem | Kısa açıklama iki |\n"
_B_GOVDE = "| Üçüncü yöntem | Kısa açıklama üç |\n| Dördüncü yöntem | Kısa açıklama dört |\n"


def _harf(s):
    return Counter(re.findall(r"[0-9A-Za-zÀ-ÿĀ-žА-я]", s))


def _say(metin):
    return mo.onar_rapor(metin)[1]


class TestBirlesir:
    def test_ingilizce_iki_etiket(self):
        md = (_A + "\n(continued)\n\nTable 3.2 (continued)\n\n"
              + _BASLIK + _B_GOVDE + "\nSonraki paragraf.\n")
        out, say = mo.onar_rapor(md)
        assert say["devam_tablosu_birlesti"] == 1
        assert say["devam_etiketi_silindi"] == 2
        assert say["yinelenen_baslik_silindi"] == 1
        assert out.count("| Yöntem adı |") == 1
        assert "continued" not in out
        assert "Dördüncü yöntem" in out and "Sonraki paragraf." in out

    def test_ocr_bozuk_mini_tablo(self):
        mini = "| <b>Table 3.2</b> | (continued) |\n|---|---|\n| 1 able 3.2 | Commuea |\n"
        md = _A + "\n(continued)\n\n" + mini + "\n" + _BASLIK + _B_GOVDE
        out, say = mo.onar_rapor(md)
        assert say["devam_tablosu_birlesti"] == 1
        assert say["devam_etiketi_silindi"] == 2
        assert "Commuea" not in out and out.count("| Yöntem adı |") == 1

    def test_almanca_ve_rusca_etiket(self):
        for etiket in ("Tabelle 2.1 (Fortsetzung)", "Таблица 4 (продолжение)",
                       "Tableau 1.3 (suite)", "Tabla 2 (continuación)"):
            md = _A + "\n" + etiket + "\n\n" + _BASLIK + _B_GOVDE
            assert _say(md)["devam_tablosu_birlesti"] == 1, etiket

    def test_bolunmus_son_satir_onceki_satira_katilir(self):
        a = _BASLIK + "| Birinci yöntem | Bu açıklama sayfanın sonunda |\n"
        b = "| | yarıda kesildi ve burada sürüyor |\n| İkinci yöntem | Tam açıklama |\n"
        out, say = mo.onar_rapor(a + "\n(continued)\n\n" + _BASLIK + b)
        assert say["devam_tablosu_birlesti"] == 1
        assert say["tablo_satiri_birlesti"] >= 1
        satir = [s for s in out.splitlines() if "Birinci yöntem" in s][0]
        assert "sayfanın sonunda" in satir and "burada sürüyor" in satir

    def test_silinen_yalniz_artiklar(self):
        etiketler = "(continued)\n\nTable 3.2 (continued)\n\n"
        md = _A + "\n" + etiketler + _BASLIK + _B_GOVDE
        out = mo.onar(md)
        silinen = _harf(md) - _harf(out)
        beklenen = _harf("continued Table 3.2 continued") + _harf("Yöntem adı Ne sağlar")
        assert silinen == beklenen
        assert not (_harf(out) - _harf(md))
        assert mo.onar(out) == out


class TestDokunulmaz:
    def test_etiket_yoksa_birlesmez(self):
        md = _A + "\n" + _BASLIK + _B_GOVDE
        assert _say(md)["devam_tablosu_birlesti"] == 0

    def test_baslik_farkliysa_birlesmez(self):
        baska = "| Başka sütun | Farklı |\n|---|---|\n" + _B_GOVDE
        md = _A + "\n(continued)\n\n" + baska
        out, say = mo.onar_rapor(md)
        assert say["devam_tablosu_birlesti"] == 0
        assert "(continued)" in out

    def test_duzyazidaki_etiket_tablosuz_kalir(self):
        md = "Birinci paragraf.\n\n(continued)\n\nİkinci paragraf.\n"
        out, say = mo.onar_rapor(md)
        assert say["devam_etiketi_silindi"] == 0
        assert "(continued)" in out


class TestGercekAkis:
    """V1 bulgusu (5 Ekim): dönüştürücü <b>'yi marker_onarim'den ÖNCE **'ye
    çeviriyor; tanıyıcı vurgu işaretlerini atmalı (bir test kitabındaki tablo)."""

    def test_vurgulu_mini_tablo(self):
        mini = "| **Table 3.2** | (continued) |\n|---|---|\n| 1 able 3.2 | Commuea |\n"
        md = _A + "\n(continued)\n\n" + mini + "\n" + _BASLIK + _B_GOVDE
        out, say = mo.onar_rapor(md)
        assert say["devam_tablosu_birlesti"] == 1
        assert "Commuea" not in out

    def test_vurgulu_devam_altyazisi(self):
        md = _A + "\n**Table 3.2 (continued)**\n\n" + _BASLIK + _B_GOVDE
        assert _say(md)["devam_tablosu_birlesti"] == 1

    def test_donusturucuden_gecerek(self, tmp_path):
        import fitz
        from latex_pipeline.config import PipelineConfig
        from latex_pipeline.converter import MarkdownToLatex
        pdf = tmp_path / "source_book.pdf"
        doc = fitz.open(); doc.new_page().insert_text((72, 72), "Tablo"); doc.save(str(pdf))
        mini = "| <b>Table 3.2</b> | (continued) |\n|---|---|\n| 1 able 3.2 | Commuea |\n"
        md = "# Bölüm\n\nGiriş paragrafı.\n\n" + _A + "\n(continued)\n\n" + mini + "\n" + _BASLIK + _B_GOVDE
        tex = MarkdownToLatex(PipelineConfig(input_pdf=str(pdf), document_class="book")).convert(md)
        assert "Commuea" not in tex
        assert tex.count("Yöntem adı") == 1
