"""G3b (2026-10-05): şablon, kapak, yazı tipleri.

Kalemler:
  (1) Başlıklarda ve özel adlarda tireleme; eş aralıklı yazıda tire yok;
      listings Türkçe harf (L-1) doğrulaması.
  (2) Otomatik kapak (Berke kararı): kaynak PDF'in 1. sayfası görsel
      ağırlıklıysa basılır, metin sayfasıysa (ya da boşsa) basılmaz.
  (3) Sayfa numarası iki kez başlıyor (mt okuma MT-27).
  (4) Tablo altyazısı tablo çizgisine değiyor (mt okuma MT-25).
  (5) Yazı tipleri: STIX Two Text kalın; Latin Modern metin + matematik.

Derleme testleri gerçek XeLaTeX ile koşar (yoksa atlanır).
"""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import fitz  # PyMuPDF
import pytest

from latex_pipeline.config import PipelineConfig
from latex_pipeline.template_engine import (
    TemplateEngine,
    insert_front_notice,
)

_XELATEX = shutil.which("xelatex")
_PDFFONTS = shutil.which("pdffonts")
_PDFTOTEXT = shutil.which("pdftotext")

needs_xelatex = pytest.mark.skipif(_XELATEX is None, reason="xelatex yok")


# ---------------------------------------------------------------------------
# Yardımcılar
# ---------------------------------------------------------------------------

def _compile(tmp_path: Path, name: str, tex: str, passes: int = 1) -> Path:
    src = tmp_path / f"{name}.tex"
    src.write_text(tex, encoding="utf-8")
    for _ in range(passes):
        subprocess.run(
            [_XELATEX, "-interaction=nonstopmode", "-halt-on-error",
             "-file-line-error", src.name],
            cwd=tmp_path, capture_output=True, timeout=300,
        )
    return tmp_path / f"{name}.pdf"


def _log(pdf: Path) -> str:
    return pdf.with_suffix(".log").read_text(encoding="utf-8", errors="replace")


def _errors(log: str) -> list:
    return [l for l in log.splitlines()
            if re.match(r"^\.?/?[^ ]*\.tex:\d+: ", l) or l.startswith("! ")]


def _missing(log: str) -> list:
    return [l for l in log.splitlines() if "Missing character" in l]


def _fonts(pdf: Path) -> str:
    return subprocess.run([_PDFFONTS, str(pdf)], capture_output=True,
                          text=True, timeout=60).stdout


def _text(pdf: Path) -> str:
    return subprocess.run([_PDFTOTEXT, "-layout", str(pdf), "-"],
                          capture_output=True, text=True, timeout=60).stdout


def _wrap(body: str, **cfg_kwargs) -> str:
    cfg = PipelineConfig(document_class="book")
    for key, value in cfg_kwargs.items():
        setattr(cfg, key, value)
    return TemplateEngine(cfg).wrap_content(body)


# Sentetik PDF'ler (ölçülen korpus durumlarının benzetimi) -------------------

_WORDS = ("alpha beta gamma delta epsilon zeta theta kappa lambda omicron "
          "sigma upsilon omega river mountain valley ocean forest desert").split()


def _text_lines(page, n_words: int, y0: float = 60, fontsize: float = 9):
    words = [_WORDS[k % len(_WORDS)] for k in range(n_words)]
    y = y0
    for k in range(0, len(words), 10):
        page.insert_text((40, y), " ".join(words[k:k + 10]), fontsize=fontsize)
        y += 11
        if y > page.rect.height - 40:
            break


def _pixmap(color=(200, 30, 30), w=40, h=60):
    pix = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, w, h), False)
    pix.set_rect(pix.irect, color)
    return pix


def _make_pdf(path: Path, first: str, n_body: int = 5, body: str = "text",
              first_words: int = 0, body_words: int = 300) -> str:
    """first/body: 'text' | 'cover' (tam sayfa görsel) | 'logo' (küçük
    görsel + metin) | 'blank' | 'scan' (tam sayfa açık renk görsel + metin)."""
    doc = fitz.open()
    w, h = 420, 595
    for i in range(1 + n_body):
        kind = first if i == 0 else body
        n = first_words if i == 0 else body_words
        page = doc.new_page(width=w, height=h)
        if kind == "cover":
            page.insert_image(page.rect, pixmap=_pixmap())
        elif kind == "logo":
            page.insert_image(fitz.Rect(40, 30, 110, 60), pixmap=_pixmap())
        elif kind == "scan":
            # Taranmış sayfa: kâğıt beyazına yakın tam sayfa görsel; metin
            # katmanı (OCR) görselin üstünde.
            page.insert_image(page.rect, pixmap=_pixmap((250, 250, 248)))
        if kind != "blank" and n:
            _text_lines(page, n)
    doc.save(str(path))
    doc.close()
    return str(path)


# ---------------------------------------------------------------------------
# (2) Otomatik kapak
# ---------------------------------------------------------------------------

def _karar(pdf):
    from latex_pipeline.kapak import kapak_karari
    return kapak_karari(pdf)


class TestKapakSentetik:
    def test_tam_sayfa_gorsel_kapak(self, tmp_path):
        pdf = _make_pdf(tmp_path / "a.pdf", "cover", first_words=12)
        k = _karar(pdf)
        assert k.basilir is True and k.ilk_gorsel > 0.9

    def test_logolu_metin_sayfasi_basilmaz(self, tmp_path):
        # Rapor benzeri: küçük logo + başlık/tarih/beyan metni.
        pdf = _make_pdf(tmp_path / "b.pdf", "logo", first_words=44)
        assert _karar(pdf).basilir is False

    def test_seyrek_metin_baslik_sayfasi_basilmaz(self, tmp_path):
        pdf = _make_pdf(tmp_path / "c.pdf", "text", first_words=19)
        assert _karar(pdf).basilir is False

    def test_bos_sayfa_basilmaz(self, tmp_path):
        pdf = _make_pdf(tmp_path / "d.pdf", "blank")
        k = _karar(pdf)
        assert k.basilir is False and "boş" in k.neden

    def test_tek_sayfa_kapak(self, tmp_path):
        pdf = _make_pdf(tmp_path / "e.pdf", "cover", n_body=0)
        assert _karar(pdf).basilir is True

    def test_tek_sayfa_metin(self, tmp_path):
        pdf = _make_pdf(tmp_path / "f.pdf", "text", n_body=0, first_words=40)
        assert _karar(pdf).basilir is False

    def test_gorselli_govde_az_sozcuklu_ilk_sayfa_kapak(self, tmp_path):
        # Bütün sayfaları koyu görselli belge (görsel ayırt etmez): az
        # sözcüklü 1. sayfa kapaktır, gövde kadar metinli olan değildir.
        pdf = _make_pdf(tmp_path / "g.pdf", "cover", body="cover",
                        first_words=10, body_words=300)
        assert _karar(pdf).basilir is True
        pdf2 = _make_pdf(tmp_path / "h.pdf", "cover", body="cover",
                         first_words=280, body_words=300)
        assert _karar(pdf2).basilir is False

    def test_taranmis_metin_sayfasi_basilmaz(self, tmp_path):
        # Taranmış kitap: kâğıt beyazı görsel mürekkep sayılmaz.
        pdf = _make_pdf(tmp_path / "i.pdf", "scan", body="scan",
                        first_words=280, body_words=300)
        assert _karar(pdf).basilir is False

    def test_bozuk_pdf_karar_yok_eski_davranis(self, tmp_path):
        bozuk = tmp_path / "bozuk.pdf"
        bozuk.write_bytes(b"%PDF-1.4 deneme")
        assert _karar(str(bozuk)).basilir is None
        tex = TemplateEngine(PipelineConfig()).wrap_content(
            "Gövde", cover_pdf_path=str(bozuk))
        assert "\\includepdf[pages={1}]" in tex

    def test_sablon_karari_uygular(self, tmp_path):
        kapak = _make_pdf(tmp_path / "k.pdf", "cover", first_words=12)
        metin = _make_pdf(tmp_path / "m.pdf", "logo", first_words=44)
        eng = TemplateEngine(PipelineConfig())
        assert "\\includepdf[pages={1}]" in eng.wrap_content(
            "Gövde", cover_pdf_path=kapak)
        assert "\\includepdf" not in eng.wrap_content(
            "Gövde", cover_pdf_path=metin)


# ---------------------------------------------------------------------------
# (3) Sayfa numarası: ön sayfalar roma rakamı
# ---------------------------------------------------------------------------

_KITAP_BASI = (
    "\\begin{titlepage}\n\\centering Kitabın Adı\n\\end{titlepage}\n\n"
    "\\tableofcontents\n\n\\mainmatter\n\n"
    "\\chapter{Birinci}\nMetin.\n\n\\chapter{İkinci}\nMetin.\n"
)
# Dönüştürücü ana maddeye geçişi yanlış yerde yaptı (numaralı bölümlerden
# SONRA).
_YANLIS_GECIS = (
    "\\chapter{Birinci}\nMetin.\n\n\\section{Alt}\nMetin.\n\n"
    "\\chapter{İkinci}\nMetin.\n\n"
    "\\tableofcontents\n\n\\mainmatter\n\n\\chapter{Üçüncü}\nMetin.\n"
)


class TestOnSayfalar:
    def test_frontmatter_eklenir(self):
        tex = _wrap(_KITAP_BASI)
        body = tex[tex.index("\\begin{document}"):]
        assert body.index("\\frontmatter") < body.index("\\begin{titlepage}")

    def test_frontmatter_zaten_varsa_eklenmez(self):
        tex = _wrap("\\frontmatter\nÖn.\n\n" + _KITAP_BASI)
        assert tex.split("\\begin{document}")[1].count("\\frontmatter") == 1

    def test_mainmatter_yoksa_eklenmez(self):
        tex = _wrap("\\chapter{Bir}\nMetin.\n")
        assert "\\frontmatter" not in tex.split("\\begin{document}")[1]

    def test_yanlis_gecis_numarali_bolum_varsa_eklenmez(self):
        tex = _wrap(_YANLIS_GECIS)
        assert "\\frontmatter" not in tex.split("\\begin{document}")[1]

    def test_kod_ve_yorumdaki_mainmatter_sayilmaz(self):
        govde = ("% \\mainmatter burada değil\n"
                 "\\begin{verbatim}\n\\mainmatter\n\\end{verbatim}\n"
                 "\\chapter{Bir}\nMetin.\n")
        tex = _wrap(govde)
        assert "\\frontmatter" not in tex.split("\\begin{document}")[1]

    def test_article_sinifinda_eklenmez(self):
        tex = _wrap(_KITAP_BASI.replace("\\chapter", "\\section"),
                    document_class="article")
        assert "\\frontmatter" not in tex.split("\\begin{document}")[1]

    def test_kapak_ve_kunye_sirasi(self, tmp_path):
        kapak = _make_pdf(tmp_path / "k.pdf", "cover", first_words=12)
        tex = TemplateEngine(PipelineConfig(document_class="book")).wrap_content(
            _KITAP_BASI, cover_pdf_path=kapak)
        body = tex[tex.index("\\begin{document}"):]
        assert body.index("\\frontmatter") < body.index("\\includepdf")
        out = insert_front_notice(tex, ["Makine çevirisi", "Satır"])
        b2 = out[out.index("\\begin{document}"):]
        # Künye kapağın arkasında, İçindekiler'den önce (kapak basılınca
        # yeniden kurulmuş başlık sayfası kalkar).
        assert (b2.index("\\includepdf") < b2.index("Makine çevirisi")
                < b2.index("\\tableofcontents"))

    def test_kunye_kapaksiz_on_sayfalarda(self):
        tex = _wrap("\\tableofcontents\n\n\\mainmatter\n\n\\chapter{Bir}\nX\n")
        out = insert_front_notice(tex, ["Makine çevirisi", "Satır"])
        b2 = out[out.index("\\begin{document}"):]
        assert b2.index("\\frontmatter") < b2.index("Makine çevirisi")

    @needs_xelatex
    def test_derleme_on_sayfa_roma_ana_madde_arap(self, tmp_path):
        pdf = _compile(tmp_path, "onsayfa", _wrap(_KITAP_BASI), passes=2)
        assert _errors(_log(pdf)) == []
        doc = fitz.open(str(pdf))
        labels = [p.get_label() for p in doc]
        doc.close()
        ilk_bolum = next(i for i, lb in enumerate(labels) if lb == "1")
        assert ilk_bolum >= 1, labels
        # Ana maddeden önceki her sayfa roma rakamı; "1" bir kez.
        assert all(re.fullmatch(r"[ivxlc]+", lb) for lb in labels[:ilk_bolum]), labels
        assert labels.count("1") == 1, labels

    @needs_xelatex
    def test_derleme_yanlis_gecis_numara_tekrarlamaz(self, tmp_path):
        pdf = _compile(tmp_path, "yanlis", _wrap(_YANLIS_GECIS), passes=2)
        assert _errors(_log(pdf)) == []
        doc = fitz.open(str(pdf))
        labels = [p.get_label() for p in doc]
        doc.close()
        # hyperref düz 1..n numarada /PageLabels yazmaz (etiket boş).
        if not any(labels):
            labels = [str(i + 1) for i in range(len(labels))]
        assert len(labels) == len(set(labels)), labels


# ---------------------------------------------------------------------------
# (1) Tireleme: başlıklar, özel adlar, eş aralıklı yazı; listings (L-1)
# ---------------------------------------------------------------------------

_DAR = "\\newgeometry{textwidth=4.2cm}\n"
_OZEL_ADLAR = (
    "Ajanlar VexaAI QuarryLabs Packagerium STEPNINE TIGHTCONTROL "
    "HuggingFace Kubernetes Oakmont Cloudflare sistemlerinde çalıştı; "
    "VexaAI ile QuarryLabs ve Packagerium kayıtları STEPNINE belgesinde "
    "Kubernetes Cloudflare HuggingFace Oakmont adlarıyla anılıyordu. "
) * 3


def _satir_sonu_bolmeleri(text: str) -> list:
    """pdftotext çıktısında tireyle biten satırların son sözcükleri."""
    out = []
    for line in text.splitlines():
        s = line.rstrip()
        if s.endswith("-") and not s.endswith("--"):
            out.append(s.split()[-1])
    return out


@pytest.mark.skipif(_XELATEX is None or _PDFTOTEXT is None,
                    reason="xelatex/pdftotext yok")
class TestTireleme:
    def test_ozel_adlar_bolunmez(self, tmp_path):
        pdf = _compile(tmp_path, "ozel", _wrap(_DAR + _OZEL_ADLAR))
        assert _errors(_log(pdf)) == []
        bolmeler = _satir_sonu_bolmeleri(_text(pdf))
        buyuk = [b for b in bolmeler if b[:1].isupper()]
        assert buyuk == [], bolmeler

    def test_baslikta_tireleme_yok(self, tmp_path):
        govde = (_DAR
                 + "\\chapter{Değerlendirmenin sınıflandırılması ve "
                   "belirlenmesi}\nMetin.\n\n"
                 + "\\section{Kritik bildirimlerin saptanması ve görev "
                   "alanına göre sınıflandırılması}\nMetin.\n\n"
                 + "\\subsection{Karşılaştırılabilirliğin "
                   "değerlendirilmesi}\nMetin.\n\n"
                 + "\\paragraph{Uluslararasılaştırılmış "
                   "kayıtların incelenmesi}\nMetin.\n")
        pdf = _compile(tmp_path, "baslik", _wrap(govde))
        assert _errors(_log(pdf)) == []
        assert _satir_sonu_bolmeleri(_text(pdf)) == []

    def test_paragraph_basligi_ayri_satirda(self, tmp_path):
        govde = "\\paragraph{Ara Başlık}\nGövde metni burada.\n"
        pdf = _compile(tmp_path, "para", _wrap(govde))
        lines = [l.strip() for l in _text(pdf).splitlines() if l.strip()]
        assert "Ara Başlık" in lines, lines[:5]

    def test_es_aralikli_yazida_tire_yok(self, tmp_path):
        govde = (_DAR + ("Belirteç \\texttt{zzinboxhblosckarsilastirilabilirlik"
                         "degerlendirilmesi} ve \\texttt{monitortargetfirstflag"
                         "poisonedkayitlari} günlükte. ") * 3)
        pdf = _compile(tmp_path, "mono", _wrap(govde))
        assert _errors(_log(pdf)) == []
        text = _text(pdf)
        assert "zzinboxhblosc-" not in text
        mono = [b for b in _satir_sonu_bolmeleri(text)
                if b.startswith(("zzin", "monitor", "kars", "deger", "poison"))]
        assert mono == [], mono

    def test_uzun_buyuk_harfli_ad_satiri_tasirmaz(self, tmp_path):
        # \uchyph=0 ile uzun özel ad bölünemez; bir test kitabının
        # kaynakçasında 14 pt taşma oldu (uzun bir soyadı). Son çare esnetmesi
        # (6em) satırı taşırmadan dizer (gerileme koruması). Girdiler
        # sentetiktir (aynı biçim, aynı uzunlukta ad); ölçüldü:
        # \emergencystretch=0pt iken 77. girdinin ilk satırı taşar (25,9 pt).
        govde = ("\\newgeometry{textwidth=9cm}\n\\begin{itemize}\n"
                 "\\item[77.] G. Vandenbergen, E. Hoffstrom, D.J. Moreira, "
                 "S. Lindahl, B. Vandermeulenberg, Invertible transport maps "
                 "for density estimation and sampling. J. Comput. Stat. Learn. "
                 "\\textbf{22}, 2617–2680 (2021)\n"
                 "\\item[78.] Lin Y-H, Branford V, Costa Ferreira K, "
                 "Halvorsen S (2012) Nonstationarity, irreversibility, "
                 "intractability, and instability in monetary and pricing "
                 "models. Dynamics 18(1):51–55\n\\end{itemize}\n")
        pdf = _compile(tmp_path, "kaynakca", _wrap(govde))
        log = _log(pdf)
        assert _errors(log) == []
        assert "Overfull \\hbox" not in log

    def test_url_harfleri_esnemez(self, tmp_path):
        # xurl \Urlmuskip = 0mu plus 1mu: gevşek satırda URL harf harf
        # açılıyordu (bir test kitabında "h t t p s : / / c o l a b ...").
        # Kırılma kalır, esneme kalkar.
        govde = ("\\typeout{KCURL=\\the\\Urlmuskip;}\n"
                 "\\newgeometry{textwidth=5cm}\n"
                 "Kaynak: \\url{https://colab.research.google.com/drive/"
                 "abcdefghijklmnopqrstuvwxyz} adresinde.\n")
        pdf = _compile(tmp_path, "url", _wrap(govde))
        log = _log(pdf)
        assert _errors(log) == []
        assert "KCURL=0.0mu;" in log, re.findall(r"KCURL=[^;]*;", log)
        assert "h t t p" not in _text(pdf)

    def test_simge_ve_emoji_basilir(self, tmp_path):
        # Önceki emoji kuralı (R1) genelleştirildi: ✅ ❌ (Noto Emoji), ✓ ✗ ★ (FreeSerif
        # düz kesimi) her biçemde basılır; eskiden PDF'te boş kalıyordu.
        dizi = "✅ ❌ ✓ ✗ ★ ☑ ⬆ 🚀 ✔ ⚠"
        govde = (f"{dizi}\n\n\\textbf{{{dizi}}}\n\n\\textit{{{dizi}}}\n\n"
                 f"\\texttt{{{dizi}}}\n\nbaşarılı ✅! hatalı ❌: son.\n")
        pdf = _compile(tmp_path, "simge", _wrap(govde))
        log = _log(pdf)
        assert _errors(log) == []
        assert _missing(log) == [], _missing(log)[:5]
        assert "başarılı ✅! hatalı ❌: son." in _text(pdf)

    def test_listings_turkce_harfler(self, tmp_path):
        # L-1: listings XeLaTeX'te ğ ş ı İ Ğ Ş'yi bozuyordu; lstlisting artık
        # fvextra Verbatim ile dizilir (doğrulama).
        govde = ("\\begin{lstlisting}[language=Python]\n"
                 "# değiştirebilir: ığüşöç İĞÜŞÖÇ\nx = 1\n"
                 "\\end{lstlisting}\n")
        pdf = _compile(tmp_path, "lst", _wrap(govde))
        assert _errors(_log(pdf)) == []
        text = _text(pdf)
        assert "değiştirebilir" in text and "İĞÜŞÖÇ" in text


# ---------------------------------------------------------------------------
# (4) Tablo çizgisi ile bitişik altyazı
# ---------------------------------------------------------------------------

# Sanitizer'ın (Phase 6, _glued_unit) ürettiği birim: dış altyazı paragrafı
# ile tablo içeriği arada boşluk olmadan aynı kutuda.
def _glued(caption_first: bool) -> str:
    tablo = ("{\\centering\n\\begin{tabular}{l l l}\n\\toprule\n"
             "Malzeme & yoğunluk & iletkenlik \\\\\n\\midrule\n"
             "Bakır & 8930 & 399 \\\\\nBeton & 2300 & 1,40 \\\\\n"
             "\\bottomrule\n\\end{tabular}\\par}")
    alt = "\\textbf{Tablo 1.1:} Yoğunluk ve iletkenlik gypq değerleri"
    parts = [alt, tablo] if caption_first else [tablo, alt]
    return ("\\par\\setbox0\\vbox{%\n" + "\n\n".join(parts) + "\\par}%\n"
            "\\ifdim\\dimexpr\\ht0+\\dp0+\\textfloatsep\\relax>\\textheight"
            "\\unvbox0 \\else"
            "\\begin{table}[!htb]\\box0 \\end{table}\\fi\\par\n")


def _rules_and_caption(pdf: Path):
    doc = fitz.open(str(pdf))
    page = doc[0]
    rules = [d["rect"] for d in page.get_drawings()
             if d["rect"].width > 40 and d["rect"].height < 2]
    cap = [fitz.Rect(w[:4]) for w in page.get_text("words")
           if w[4] in ("Tablo", "1.1:", "gypq")]
    doc.close()
    return sorted(rules, key=lambda r: r.y0), cap


@needs_xelatex
class TestTabloAltyazi:
    def test_ustteki_altyazi_ust_cizgiye_degmez(self, tmp_path):
        pdf = _compile(tmp_path, "ust", _wrap(_glued(True)))
        assert _errors(_log(pdf)) == []
        rules, cap = _rules_and_caption(pdf)
        assert rules and cap
        gap = rules[0].y0 - max(r.y1 for r in cap)
        assert gap >= 3.0, gap

    def test_alttaki_altyazi_alt_cizgiye_degmez(self, tmp_path):
        pdf = _compile(tmp_path, "alt", _wrap(_glued(False)))
        assert _errors(_log(pdf)) == []
        rules, cap = _rules_and_caption(pdf)
        assert rules and cap
        gap = min(r.y0 for r in cap) - rules[-1].y1
        assert gap >= 3.0, gap


# ---------------------------------------------------------------------------
# (5) Yazı tipleri
# ---------------------------------------------------------------------------

@pytest.mark.skipif(_XELATEX is None or _PDFFONTS is None,
                    reason="xelatex/pdffonts yok")
class TestYaziTipleri:
    def test_stix_kalin_dizilir(self, tmp_path):
        pdf = _compile(tmp_path, "kalin", _wrap(
            "Düz metin \\textbf{kalın metin} ve \\textit{eğik} "
            "\\textbf{\\textit{kalın eğik}}.\n"))
        log = _log(pdf)
        assert _errors(log) == []
        fonts = _fonts(pdf)
        assert re.search(r"STIXTwoText-Bold\b", fonts), fonts
        assert re.search(r"STIXTwoText-BoldItalic", fonts), fonts
        assert "Font shape `TU/STIXTwoText(0)/b/n' undefined" not in log

    def test_gercek_kalin_sahtesiyle_degismez(self, tmp_path):
        # fontspec'in AutoFakeBold seçeneği, açık BoldFont verilmeyen yazı
        # tipinde gerçek kalını "embolden" ile değiştiriyordu (Helvetica
        # Neue: kalın -> embolden=2, kalın eğik -> düz eğik). Kullanılmaz.
        pdf = _compile(tmp_path, "helv", _wrap(
            "Düz \\textbf{kalın} ve \\textbf{\\textit{kalın eğik}}.\n",
            font_main="Helvetica Neue"))
        log = _log(pdf)
        assert _errors(log) == []
        fonts = _fonts(pdf)
        if "HelveticaNeue" not in fonts:
            pytest.skip("Helvetica Neue yok")
        assert "embolden" not in log
        assert "HelveticaNeue-Bold" in fonts, fonts
        assert "HelveticaNeue-BoldItalic" in fonts, fonts

    def test_kalinsiz_yazi_tipinde_sahte_kalin(self, tmp_path):
        # Tek kesimli yazı tipi: kalın kesim yok -> sahte kalın (eskiden
        # NFSS düz kesime düşüyor, \textbf görünmüyordu).
        pdf = _compile(tmp_path, "chalk", _wrap(
            "Düz \\textbf{kalın}.\n", font_main="Chalkduster"))
        log = _log(pdf)
        assert _errors(log) == []
        if "Chalkduster" not in _fonts(pdf):
            pytest.skip("Chalkduster yok")
        assert "embolden=2" in log
        assert "/b/n' undefined" not in log

    def test_latin_modern_metin_latin_modern_matematik(self, tmp_path):
        pdf = _compile(tmp_path, "lm", _wrap(
            "Metin $m F = a + \\alpha \\leq \\Longleftrightarrow \\sum_i x_i$.\n",
            font_main="Latin Modern Roman"))
        log = _log(pdf)
        assert _errors(log) == []
        assert _missing(log) == []
        fonts = _fonts(pdf)
        assert "LatinModernMath" in fonts, fonts
        assert "STIXTwoMath" not in fonts, fonts

    def test_stix_metin_stix_matematik_degismez(self, tmp_path):
        pdf = _compile(tmp_path, "stix", _wrap("Metin $m F = a + \\alpha$.\n"))
        assert _errors(_log(pdf)) == []
        assert "STIXTwoMath" in _fonts(pdf)
