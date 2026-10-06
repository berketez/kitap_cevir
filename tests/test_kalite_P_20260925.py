"""Kalite kulvarı P (2026-09-25): sayfa düzeni.

Kanıt: Yol A dil teyidi karşılaştırma raporu (§9).

- R1  Özgün PDF'in 1. sayfası KOŞULSUZ kapak yapılıyordu; bölüm/kesit
      çevirisinde (iki test kitabında) okurun ilk gördüğü sayfa çevrilmemiş
      tam metin sayfasıydı. Artık 1. sayfanın kapak olduğu ölçülür.
- R3  Kenar boşluğu ölçümü: %8'lik bant gövde bloğunu atıyor (bir kitapta üst 83 mm),
      kısa sayfalar medyana giriyordu (sentetiklerde alt 45–135 mm). Satır
      düzeyinde süs ayıklama + tam sayfa kenarı. Küçük boşlukta üst bilgi
      kâğıda sığdırılır.
- R15 Metinde ◦ √ ἡ 𝜆 gibi ana yazı tipinde olmayan glifler tofu basılıyordu;
      matematikte ondalık virgülden sonra boşluk ("998, 2").
- R4  Matematikte Kiril (p_н) ve Türkçe harfler (T_çıkış) cmmi'ye düşüp
      basılmıyordu; \\text{} içindeki Kiril ana yazı tipinde yoksa basılmıyordu.

Bütün PDF girdileri bu dosyada PyMuPDF ile üretilen SENTETİK belgelerdir.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

from latex_pipeline.config import PipelineConfig
from latex_pipeline.layout_detector import (
    PT_TO_MM,
    LayoutDetector,
    LayoutInfo,
)
from latex_pipeline.template_engine import (
    TemplateEngine,
    first_page_is_cover,
    head_foot_geometry,
)

fitz = pytest.importorskip("fitz")

_XELATEX = shutil.which("xelatex")
_MM = 72.0 / 25.4  # mm -> pt

_WORDS = ("isil iletkenlik duvar akis sicaklik fark yuzey katman direnc "
          "denge enerji tasinim").split()
_HECE = ("ka le mi so tu ra ne di po ya be ci fo gu ha").split()


def _word(seed: int) -> str:
    """Tekrarsız sözde sözcük: satırlar sayfalar arasında TEKRAR ETMEMELİ
    (tekrar eden satır üst/alt bilgi sayılır)."""
    n = len(_HECE)
    return _HECE[seed % n] + _HECE[(seed // n) % n] + _HECE[(seed * 7 + 3) % n]


def _line(n_words: int, seed: int) -> str:
    return " ".join(_word(seed * 31 + i * 5) for i in range(n_words))


# ---------------------------------------------------------------------------
# Sentetik PDF üreticisi
# ---------------------------------------------------------------------------

def _make_book(path: Path, pages, width_mm=148.0, height_mm=210.0) -> str:
    """*pages*: her sayfa için sözlük.

    anahtarlar: body_top_mm, body_lines, pitch_pt, size, header (metin|None),
    header_y_mm, folio (metin|None), folio_y_mm, title (metin|None),
    title_y_mm, left_mm, right_mm, footnote (metin|None), footnote_y_mm,
    words_per_line.
    """
    doc = fitz.open()
    w, h = width_mm * _MM, height_mm * _MM
    for i, spec in enumerate(pages):
        page = doc.new_page(width=w, height=h)
        size = spec.get("size", 10)
        left = spec.get("left_mm", 15.0) * _MM
        if spec.get("header"):
            page.insert_text((left, spec["header_y_mm"] * _MM),
                             spec["header"], fontsize=size * 0.9)
        y = spec.get("body_top_mm", 20.0) * _MM + size
        if spec.get("title"):
            page.insert_text((left, spec["title_y_mm"] * _MM + 2 * size),
                             spec["title"], fontsize=2 * size)
        pitch = spec.get("pitch_pt", 12.0)
        for k in range(spec.get("body_lines", 0)):
            page.insert_text((left, y), _line(spec.get("words_per_line", 9),
                                              i * 7 + k), fontsize=size)
            y += pitch
        if spec.get("footnote"):
            page.insert_text((left, spec["footnote_y_mm"] * _MM),
                             spec["footnote"], fontsize=size * 0.8)
        if spec.get("folio"):
            page.insert_text((w / 2, spec["folio_y_mm"] * _MM),
                             spec["folio"], fontsize=size)
    doc.save(str(path))
    doc.close()
    return str(path)


def _full_page(n, **kw):
    spec = dict(body_top_mm=20.0, body_lines=38, folio=str(n), folio_y_mm=202.0)
    spec.update(kw)
    return spec


def _measured_body_bottom_mm(pdf: str, page_index: int) -> float:
    doc = fitz.open(pdf)
    page = doc[page_index]
    ys = [b[3] for b in page.get_text("blocks") if b[6] == 0
          and not b[4].strip().isdigit()]
    h = page.rect.height
    doc.close()
    return (h - max(ys)) * PT_TO_MM


def _measured_body_top_mm(pdf: str, page_index: int, skip_header=True) -> float:
    doc = fitz.open(pdf)
    page = doc[page_index]
    ys = sorted(b[1] for b in page.get_text("blocks") if b[6] == 0)
    doc.close()
    return ys[1 if skip_header else 0] * PT_TO_MM


# ---------------------------------------------------------------------------
# R3 — kenar boşluğu ölçümü
# ---------------------------------------------------------------------------

class TestR3KenarBoslugu:
    def test_govde_ust_banda_degse_de_atilmaz(self, tmp_path):
        """Taranmış bölüm biçimi: gövde 12,5 mm'de başlıyor (eski %8 bant = 16,8 mm),
        üst bilgi yok, sayfa numarası gövdenin hemen altında aynı aralıkla."""
        pages = [dict(body_top_mm=40, body_lines=5)]  # kapak
        for n in range(2, 13):
            pages.append(dict(body_top_mm=12.5, body_lines=44, pitch_pt=11.5,
                              folio=str(n), folio_y_mm=195.5))
        pdf = _make_book(tmp_path / "jrd_benzeri.pdf", pages)
        info = LayoutDetector().detect(pdf)
        gercek_ust = _measured_body_top_mm(pdf, 3, skip_header=False)
        gercek_alt = _measured_body_bottom_mm(pdf, 3)
        assert abs(info.margin_top_mm - gercek_ust) <= 1.0, info
        assert abs(info.margin_bottom_mm - gercek_alt) <= 1.0, info

    def test_kisa_sayfalar_alt_boslugu_sisirmez(self, tmp_path):
        """Sentetik belgeler biçimi: 5 sayfalık örnekte 3 kısa sayfa
        (İçindekiler, bölüm sonu) medyanı 45–135 mm'ye çıkarıyordu."""
        pages = [dict(body_top_mm=50, body_lines=4)]  # kapak
        pages.append(dict(body_top_mm=30, body_lines=8, folio="1",
                          folio_y_mm=202))                       # İçindekiler
        pages.append(_full_page(2, header="Kitap Adi  Bolum", header_y_mm=10))
        pages.append(dict(body_top_mm=20, body_lines=9, folio="3",
                          folio_y_mm=202, header="Kitap Adi  Bolum",
                          header_y_mm=10))                       # bölüm sonu
        pages.append(_full_page(4, header="Kitap Adi  Bolum", header_y_mm=10))
        pages.append(dict(body_top_mm=20, body_lines=3, folio="5",
                          folio_y_mm=202))                       # kısa son
        pages.append(dict(body_top_mm=20, body_lines=30))        # arka
        pdf = _make_book(tmp_path / "kisa.pdf", pages)
        info = LayoutDetector().detect(pdf)
        gercek_alt = _measured_body_bottom_mm(pdf, 2)
        assert abs(info.margin_bottom_mm - gercek_alt) <= 1.0, info
        assert abs(info.margin_top_mm - 20.0) <= 1.5, info

    def test_tekrar_eden_ust_bilgi_ve_sayfa_numarasi_ayiklanir(self, tmp_path):
        pages = [dict(body_top_mm=60, body_lines=3)]
        for n in range(2, 12):
            pages.append(_full_page(n, header="Isi Aktarimi  Temel Kavramlar",
                                    header_y_mm=9))
        pdf = _make_book(tmp_path / "ustbilgi.pdf", pages)
        info = LayoutDetector().detect(pdf)
        # Üst bilgi 9 mm'de; gövde 20 mm'de başlar.
        assert abs(info.margin_top_mm - 20.0) <= 1.5, info

    def test_kenarinda_sayfa_numarasi_olan_ayrik_ust_bilgi(self, tmp_path):
        """İki sütunlu Almanca kitap biçimi: tek sayfalarda 'Baslik 5', ayrık, tekrar etmeyen."""
        pages = [dict(body_top_mm=60, body_lines=3)]
        for n in range(2, 8):
            pages.append(_full_page(n, header=f"Bolum Basligi {n} numara {n}",
                                    header_y_mm=9, folio=None))
        pdf = _make_book(tmp_path / "kenar_no.pdf", pages)
        info = LayoutDetector().detect(pdf)
        assert abs(info.margin_top_mm - 20.0) <= 1.5, info

    def test_bolum_acilis_sayfalari_ust_olcume_girmez(self, tmp_path):
        pages = [dict(body_top_mm=60, body_lines=3)]
        for n in range(2, 8):
            if n % 2 == 0:  # bölüm açılışı: başlık 40 mm'de, gövde aşağıda
                pages.append(dict(title="Bolum", title_y_mm=40,
                                  body_top_mm=70, body_lines=26,
                                  folio=str(n), folio_y_mm=202))
            else:
                pages.append(_full_page(n))
        pdf = _make_book(tmp_path / "bolumler.pdf", pages)
        info = LayoutDetector().detect(pdf)
        assert abs(info.margin_top_mm - 20.0) <= 1.5, info

    def test_tek_satirlik_dipnot_alt_bilgi_sayilmaz(self, tmp_path):
        pages = [dict(body_top_mm=60, body_lines=3)]
        for n in range(2, 12):
            pages.append(_full_page(n, body_lines=34,
                                    footnote=f"1 {_word(n * 13)} notu.",
                                    footnote_y_mm=190.0))
        pdf = _make_book(tmp_path / "dipnot.pdf", pages)
        info = LayoutDetector().detect(pdf)
        # Dipnotun alt kenarı: 210 - 190 = 20 mm (+ inişler); gövde çok yukarıda.
        assert info.margin_bottom_mm < 21.0, info

    def test_ocr_kirpintisi_alt_satir_atilir(self, tmp_path):
        pages = [dict(body_top_mm=60, body_lines=3)]
        for n in range(2, 12):
            pages.append(_full_page(n, folio="ю" if n % 3 == 0 else str(n)))
        pdf = _make_book(tmp_path / "ocr.pdf", pages)
        info = LayoutDetector().detect(pdf)
        gercek_alt = _measured_body_bottom_mm(pdf, 1)  # rakam numaralı sayfa
        assert abs(info.margin_bottom_mm - gercek_alt) <= 1.0, info

    def test_karsilikli_duzende_metin_genisligi_korunur(self, tmp_path):
        pages = [dict(body_top_mm=60, body_lines=3)]
        for n in range(2, 14):
            left = 22.0 if n % 2 else 15.0
            pages.append(_full_page(n, left_mm=left))
        pdf = _make_book(tmp_path / "karsilikli.pdf", pages)
        info = LayoutDetector().detect(pdf)
        doc = fitz.open(pdf)
        page = doc[2]
        xs = [b for b in page.get_text("blocks") if b[6] == 0
              and not b[4].strip().isdigit()]
        genislik = (max(b[2] for b in xs) - min(b[0] for b in xs)) * PT_TO_MM
        doc.close()
        olculen = info.page_width_mm - info.margin_left_mm - info.margin_right_mm
        assert abs(olculen - genislik) <= 1.0, (info, genislik)

    def test_modulde_tanimsiz_ada_cagri_yok(self):
        """2026-09-28: _strip_furniture'da `return body` sonrasında var
        olmayan _is_furniture'ı çağıran ulaşılamaz eski gövde kalmıştı."""
        import ast
        import builtins

        import latex_pipeline.layout_detector as ld

        tree = ast.parse(Path(ld.__file__).read_text(encoding="utf-8"))
        tanimli = set(dir(builtins)) | set(dir(ld))
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.ClassDef)):
                tanimli.add(node.name)
            elif isinstance(node, ast.arg):
                tanimli.add(node.arg)
            elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
                tanimli.add(node.id)
        cagrilan = {n.func.id for n in ast.walk(tree)
                    if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
        assert sorted(cagrilan - tanimli) == []

    def test_metin_yoksa_varsayilan(self, tmp_path):
        pdf = _make_book(tmp_path / "bos.pdf", [dict(), dict(), dict(), dict()])
        info = LayoutDetector().detect(pdf)
        assert (info.margin_top_mm, info.margin_bottom_mm,
                info.margin_left_mm, info.margin_right_mm) == (25.0,) * 4


class TestR3UstBilgiSigdirma:
    def test_kucuk_ust_boslukta_headsep_daralir(self):
        assert head_foot_geometry(12.8, 17.0) == ["headsep=3.6mm"]

    def test_yeterli_boslukta_degisiklik_yok(self):
        assert head_foot_geometry(22.0, 22.0) == []
        assert head_foot_geometry(34.3, 24.5) == []

    def test_cok_kucuk_boslukta_ust_bilgi_icine_alinir(self):
        opts = head_foot_geometry(8.0, 10.0)
        assert "includehead" in opts and "includefoot" in opts

    def test_kucuk_alt_boslukta_footskip_daralir(self):
        assert head_foot_geometry(20.4, 13.5) == ["footskip=8.5mm"]

    def test_geometry_blogu(self):
        info = LayoutInfo(page_width_mm=143.3, page_height_mm=206.2,
                          margin_top_mm=12.8, margin_bottom_mm=17.0,
                          margin_left_mm=13.0, margin_right_mm=11.3)
        cfg = PipelineConfig(document_class="book")
        cfg.apply_layout(info)
        tex = TemplateEngine(cfg).wrap_content("Gövde.")
        blok = tex[tex.index("\\geometry{"):]
        blok = blok[:blok.index("}") + 1]
        assert "top=12.8mm" in blok and "headsep=3.6mm" in blok
        cfg2 = PipelineConfig(document_class="book")
        cfg2.apply_layout(LayoutInfo(margin_top_mm=30.0, margin_bottom_mm=25.0))
        tex2 = TemplateEngine(cfg2).wrap_content("Gövde.")
        assert "headsep" not in tex2 and "footskip" not in tex2


# ---------------------------------------------------------------------------
# R1 — özgün kapak kararı
# ---------------------------------------------------------------------------

def _cover_pdf(tmp_path: Path, first_words: int, body_words: int = 300,
               n_body: int = 5, first_image: bool = False) -> str:
    doc = fitz.open()
    w, h = 420, 595
    for i in range(1 + n_body):
        page = doc.new_page(width=w, height=h)
        n = first_words if i == 0 else body_words
        if i == 0 and first_image:
            pix = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 40, 60), False)
            pix.set_rect(pix.irect, (120, 30, 30))
            page.insert_image(fitz.Rect(0, 0, w, h), pixmap=pix)
        words = [_WORDS[(i + k) % len(_WORDS)] for k in range(n)]
        y = 50
        for k in range(0, len(words), 10):
            page.insert_text((30, y), " ".join(words[k:k + 10]), fontsize=9)
            y += 11
            if y > h - 30:
                break
    path = tmp_path / f"kapak_{first_words}_{body_words}_{int(first_image)}.pdf"
    doc.save(str(path))
    doc.close()
    return str(path)


class TestR1Kapak:
    def test_metin_sayfasi_kapak_degil(self, tmp_path):
        pdf = _cover_pdf(tmp_path, first_words=320, body_words=340)
        assert first_page_is_cover(pdf) is False
        tex = TemplateEngine(PipelineConfig()).wrap_content(
            "\\begin{titlepage}X\\end{titlepage}\nGövde", cover_pdf_path=pdf)
        assert "\\includepdf" not in tex
        assert "\\usepackage{pdfpages}" not in tex
        # Kapak konmadıysa yeniden kurulan başlık sayfası da korunur.
        assert "\\begin{titlepage}" in tex

    def test_bolum_acilis_sayfasi_kapak_degil(self, tmp_path):
        # İki sütunlu Almanca bir kitapta: 316 sözcük, gövde medyanı 446 (oran 0,71).
        pdf = _cover_pdf(tmp_path, first_words=230, body_words=320)
        assert first_page_is_cover(pdf) is False

    # G3b (2026-10-05, Berke kararı): kapak görsel ağırlıkla ayırt edilir;
    # görselsiz (dizgiyle kurulmuş) başlık sayfası metin sayfasıdır, basılmaz
    # (içeriği çevrilmiş gövdede, Türkçe başlık sayfası kalır).
    def test_seyrek_baslik_sayfasi_kapak_degil(self, tmp_path):
        pdf = _cover_pdf(tmp_path, first_words=19)
        assert first_page_is_cover(pdf) is False
        tex = TemplateEngine(PipelineConfig()).wrap_content(
            "Gövde", cover_pdf_path=pdf)
        assert "\\includepdf" not in tex

    def test_tanitim_yazili_baslik_sayfasi_kapak_degil(self, tmp_path):
        pdf = _cover_pdf(tmp_path, first_words=90, body_words=340)
        assert first_page_is_cover(pdf) is False

    def test_gorselli_ocr_kapak(self, tmp_path):
        # Kapak görselinin OCR'ı çok sözcük üretse de görsel oranı karar verir.
        pdf = _cover_pdf(tmp_path, first_words=200, body_words=300,
                         first_image=True)
        assert first_page_is_cover(pdf) is True

    def test_karar_verilemezse_eski_davranis(self, tmp_path):
        bozuk = tmp_path / "bozuk.pdf"
        bozuk.write_bytes(b"%PDF-1.4 deneme")
        assert first_page_is_cover(str(bozuk)) is None
        tex = TemplateEngine(PipelineConfig()).wrap_content(
            "Gövde", cover_pdf_path=str(bozuk))
        assert "\\includepdf[pages={1}]" in tex

    def test_acik_sayfa_listesi_olcumu_ezer(self, tmp_path):
        pdf = _cover_pdf(tmp_path, first_words=320, body_words=340)
        tex = TemplateEngine(PipelineConfig()).wrap_content(
            "Gövde", cover_pdf_path=pdf, cover_pages=[1])
        assert "\\includepdf[pages={1}]" in tex

    def test_tek_sayfa_metin_katmani_yok(self, tmp_path):
        doc = fitz.open()
        doc.new_page()
        path = tmp_path / "bos1.pdf"
        doc.save(str(path))
        doc.close()
        # G3b: boş sayfa kapak değildir (basılırsa çevirinin ilk sayfası boş).
        assert first_page_is_cover(str(path)) is False


# ---------------------------------------------------------------------------
# R15 / R4 — şablon: glif yedeği, matematik harfleri, ondalık virgül
# ---------------------------------------------------------------------------

_GLIF_GOVDE = r"""
\chapter{Glif}
Metin: 20 ◦C, ω = √2, σελἡνη, harf 𝜆. Kiril: ракетный двигатель.
Matematik: $p_{н} = 0$, $P_{уд}$, $T_{\text{раб}}$, $T_{çıkış}$, $İ$, $ä ß é ñ œ$.
Sınırda: {\bfseries ἡ}! ve ἡ: \footnote{Dipnotta ἡ ve ◦.}
\section{Başlıkta ἡ ve √}
Son.
"""


def _wrap(body: str, font_main: str = "STIX Two Text") -> str:
    cfg = PipelineConfig(document_class="book", font_main=font_main,
                         font_math="STIX Two Math")
    return TemplateEngine(cfg).wrap_content(body)


def _compile(tmp_path: Path, name: str, tex: str) -> str:
    src = tmp_path / f"{name}.tex"
    src.write_text(tex, encoding="utf-8")
    subprocess.run(
        [_XELATEX, "-interaction=nonstopmode", "-halt-on-error",
         "-file-line-error", src.name],
        cwd=tmp_path, capture_output=True, timeout=240,
    )
    return (tmp_path / f"{name}.log").read_text(encoding="utf-8",
                                                errors="replace")


def _errors(log: str) -> list:
    return [l for l in log.splitlines()
            if re.match(r"^\.?/?[^ ]*\.tex:\d+: ", l) or l.startswith("! ")]


class TestR15SablonMetni:
    def test_bloklar_sablonda(self):
        tex = _wrap("Gövde.")
        pre = tex.split("\\begin{document}")[0]
        assert "\\newXeTeXintercharclass\\kc@fallbackclass" in pre
        assert "\\kc@mathletters{\"0400}{\"052F}{\\symkcmathcyr}" in pre
        assert "\\kc@mathletters{\"02B0}{\"02FF}{\\symkcmathlatin}" in pre
        assert "\\kc@mathletters{\"1E00}{\"1EFF}{\\symkcmathlatin}" in pre
        assert "\\futurelet\\kc@nexttoken\\kc@smartcomma" in pre
        assert "begindocument/end" in pre

    @pytest.mark.parametrize("manual", [True, False])
    def test_sanitizer_onsozu_bozmaz(self, manual):
        """Phase 6 sanitizer şablon çıktısının TAMAMINDA koşar; yeni önsöz
        (\\def, \\catcode, onaltılık sabitler) değişmeden geçmeli."""
        from latex_pipeline.sanitizer import LatexSanitizer

        tex = _wrap("Gövde: 998,2 ve ◦.")
        out, _ = LatexSanitizer(manual_translation=manual).sanitize(tex)
        assert (out.split("\\begin{document}")[0]
                == tex.split("\\begin{document}")[0])

    def test_suslu_parantez_dengesi(self):
        tex = _wrap("Gövde.")
        pre = tex.split("\\begin{document}")[0]
        # Yorumlar dışında dengeli.
        kod = "\n".join(l.split("%")[0] if not l.lstrip().startswith("%")
                        else "" for l in pre.splitlines())
        assert kod.count("{") == kod.count("}")


@pytest.mark.skipif(_XELATEX is None, reason="xelatex yok")
class TestR15R4Derleme:
    def test_eksik_glif_yok(self, tmp_path):
        log = _compile(tmp_path, "glif", _wrap(_GLIF_GOVDE))
        assert _errors(log) == []
        eksik = [l for l in log.splitlines() if "Missing character" in l]
        assert eksik == [], eksik[:5]

    def test_latin_modern_ana_yazi_tipinde_yunanca_kiril(self, tmp_path):
        """Ana yazı tipi Latin Modern'e düştüğünde (Yunanca/Kiril yok) metin
        ve \\text{} içindeki Kiril yedek yazı tipinden basılır."""
        govde = (_GLIF_GOVDE + "\nYunanca α β π ∆ ≤ ≥ ′ ℝ ve $T_{\\text{раб}}$.\n")
        log = _compile(tmp_path, "lm", _wrap(govde, font_main="Latin Modern Roman"))
        assert _errors(log) == []
        eksik = [l for l in log.splitlines() if "Missing character" in l]
        assert eksik == [], eksik[:5]

    def test_ondalik_virgul(self, tmp_path):
        govde = r"""
\makeatletter
\sbox0{$998,2$}\sbox2{$998{,}2$}
\sbox4{$f(x,y)$}\sbox6{$f(x{,}y)$}
\sbox8{$k = 1, 2$}\sbox10{$k = 1{,}2$}
\typeout{KCOLCU ondalik=\the\dimexpr\wd0-\wd2\relax;
 cift=\the\dimexpr\wd4-\wd6\relax; liste=\the\dimexpr\wd8-\wd10\relax;}
\makeatother
Gövde.
"""
        log = _compile(tmp_path, "virgul", _wrap(govde))
        assert _errors(log) == []
        m = re.search(r"KCOLCU ondalik=([-\d.]+)pt;\s*cift=([-\d.]+)pt;\s*"
                      r"liste=([-\d.]+)pt;", log.replace("\n", ""))
        assert m, log[-2000:]
        ondalik, cift, liste = (float(x) for x in m.groups())
        assert ondalik == 0.0          # "998,2": virgülden sonra boşluk yok
        assert cift > 0.5              # "f(x, y)": noktalama boşluğu korunur
        assert liste > 0.5             # "1, 2": boşluklu liste korunur

    @pytest.mark.parametrize("font_main", ["STIX Two Text", "Latin Modern Roman"])
    def test_matematikte_aralik_degistirici_ve_latin_ek(self, tmp_path, font_main):
        """2026-09-28: İngilizce bir test kitabında Marker şapkayı harfin yanına
        ayrı yazıyor ("gˆ_{1}"); matematikteki ˆ cmmi7'de yoktu. Aralık
        değiştirici harfler (U+02B0–02FF) ve Latin genişletilmiş ek
        (U+1E00–1EFF) de metin yazı tipinden basılır."""
        govde = ("\\chapter{Deneme}\nMetin ˆn ve $\\hat{g}_{1}$, $gˆ_{1}$, "
                 "$x^{ˆ}$, $mẍ + cẋ$, $ʹ$ ve $a_{ˇ}$.\n")
        log = _compile(tmp_path, "degistirici", _wrap(govde, font_main=font_main))
        assert _errors(log) == []
        eksik = [l for l in log.splitlines() if "Missing character" in l]
        assert eksik == [], eksik[:5]

    def test_ingilizce_bolumde_derleme_bozulmaz(self, tmp_path):
        govde = ("ἡ ve \\textenglish{English √ ἡ text} sonra ἡ √.\n"
                 "\\begin{tabular}{cc} ἡ & √ \\\\ ◦ & a \\end{tabular}\n"
                 "\\MakeUppercase{ἡ çı}\n")
        log = _compile(tmp_path, "ing", _wrap(govde))
        assert _errors(log) == []
