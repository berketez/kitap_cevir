"""T1 (2026-10-05): açık yayın ayıklaması — ölü kod, kişisel yollar, telifli dosya.

Kalemler (KARARLAR 26, 27):
  (1)-(2) Giriş noktalarından (app.py, 1_cikar.py, 2_derle.py,
      translate_book.py, yol_a_ortak.py, ui/, packaging/) erişilmeyen eski
      modüller silindi; son hâlleri git etiketi ``arsiv/olu-kod-20261005``
      üzerinde. Hiçbir kaynak dosya onları içe aktarmaz (fonksiyon içi tembel
      içe aktarma ve ``importlib.import_module("...")`` dahil).
  (3) Kaynak kodda, testlerde ve açık yayına giren belgelerde kişisel sabit
      yol (yazarın ev klasörü) yok.
  (4) Depoda e-kitap dosyası yok (telifli EPUB silindi; KARAR 26).
  (5) İç belgeler ``.gitattributes`` içinde ``export-ignore`` ile açık anlık
      görüntüden (``git archive``) dışlanır; ``output/`` ve ``models/``
      ``.gitignore``'dadır.

Testler git, model, XeLaTeX ya da output/ içeriği gerektirmez; açık anlık
görüntüde (``git archive`` çıktısı, .git yok) de koşar.
"""
from __future__ import annotations

import ast
import fnmatch
import runpy
import sys
import warnings
from pathlib import Path

import pytest

PROJE = Path(__file__).resolve().parent.parent

# Silinen ölü kod (KARAR 27). Yol ve modül adı aynı listeden türer.
SILINEN_YOLLAR = (
    "pdf_engine",
    "scripts",
    "dev_tools",
    "core/book_translator_v2.py",
    "core/pdf_reconstructor.py",
    "core/visual_qa.py",
    "core/category_detector.py",
    "core/exceptions.py",
    "run_full_pipeline.py",
    "exporters/pdf_overlay_exporter.py",
)
SILINEN_MODULLER = tuple(
    y[:-3].replace("/", ".") if y.endswith(".py") else y for y in SILINEN_YOLLAR)

# Açık yayına girmeyen iç belgeler (KARAR 27): depoda kalır, git archive'a girmez.
IC_BELGE_DESENLERI = (
    "/docs/archive/",
    "/kutuphane/*.md",
    "/reports/",
    "/GELECEK_PLANI.md",
    "/TODO.md",
    "/KURALLAR.md",
)

# Depoya hiç girmeyen yerel klasörler (.gitignore) ve araç önbellekleri.
_YEREL_KLASORLER = {
    "venv", "output", "models", "cevrilenkitaplar", "__pycache__",
    ".git", ".pytest_cache", ".playwright-mcp",
    # kutuphane/: izlenen içeriği yalnız iç belgeler (*.md, export-ignore);
    # alt klasörleri .gitignore'da (kutuphane/*/, telifli kitap çalışmaları).
    "kutuphane",
}
# .gitignore'daki makineye özgü ayar dosyaları (yerel model yolları taşıyabilir).
_YEREL_DOSYALAR = {
    "config/mt_selection.json", "config/mt_sources.json", "config/mt_engines.json",
}
# Kişisel yol taranan metin dosyası türleri: kod, test, betik, ayar, belge.
_METIN_UZANTILARI = {
    ".py", ".sh", ".swift", ".js", ".html", ".css", ".tex", ".toml", ".ini",
    ".cfg", ".json", ".md", ".txt", ".yml", ".yaml", ".sablon",
}
# E-kitap biçimleri (KARAR 26: açık depoda telifli kitap yok; kılavuz PDF'i
# kendi belgemiz olduğu için PDF bu listede değil).
_E_KITAP_UZANTILARI = {".epub", ".mobi", ".azw", ".azw3", ".djvu", ".fb2"}


def _goreli(p: Path) -> str:
    return p.relative_to(PROJE).as_posix()


def _export_ignore_mu(rel: str) -> bool:
    """``IC_BELGE_DESENLERI`` (kök bağlı .gitattributes desenleri) eşleşmesi."""
    for desen in IC_BELGE_DESENLERI:
        d = desen.lstrip("/")          # desenler kök bağlı (baştaki "/")
        if d.endswith("/"):            # klasör deseni: altındaki her dosya
            if rel.startswith(d):
                return True
        elif rel.count("/") == d.count("/") and fnmatch.fnmatchcase(rel, d):
            return True                # "*" yol ayırıcısını aşmaz (git gibi)
    return False


def _proje_dosyalari():
    """Açık yayına girebilecek dosyalar (yerel klasörler ve iç belgeler hariç)."""
    yigin = [PROJE]
    while yigin:
        klasor = yigin.pop()
        for p in sorted(klasor.iterdir()):
            if p.is_dir():
                if p.name not in _YEREL_KLASORLER:
                    yigin.append(p)
                continue
            rel = _goreli(p)
            if rel in _YEREL_DOSYALAR or _export_ignore_mu(rel):
                continue
            yield p


def _ice_aktarilan_moduller(agac: ast.AST, rel: str):
    """Dosyanın içe aktardığı modül adları (göreli içe aktarma çözülür)."""
    paket = rel.split("/")[:-1]
    for dugum in ast.walk(agac):
        if isinstance(dugum, ast.Import):
            for ad in dugum.names:
                yield dugum.lineno, ad.name
        elif isinstance(dugum, ast.ImportFrom):
            if dugum.level:
                taban = paket[:len(paket) - dugum.level + 1]
                modul = ".".join(taban + ([dugum.module] if dugum.module else []))
            else:
                modul = dugum.module or ""
            yield dugum.lineno, modul
            for ad in dugum.names:
                yield dugum.lineno, f"{modul}.{ad.name}" if modul else ad.name
        elif (isinstance(dugum, ast.Call) and dugum.args
              and isinstance(dugum.args[0], ast.Constant)
              and isinstance(dugum.args[0].value, str)):
            islev = dugum.func
            ad = (islev.attr if isinstance(islev, ast.Attribute)
                  else getattr(islev, "id", ""))
            if ad in ("import_module", "__import__"):
                yield dugum.lineno, dugum.args[0].value


# ---------------------------------------------------------------------------
# (1)-(2) Ölü kod
# ---------------------------------------------------------------------------

class TestOluKod:
    @pytest.mark.parametrize("rel", SILINEN_YOLLAR)
    def test_silindi(self, rel):
        assert not (PROJE / rel).exists(), (
            f"{rel} ölü kod olarak silinmişti (KARAR 27; arşiv: "
            f"arsiv/olu-kod-20261005) — geri geldi")

    def test_hicbir_dosya_silinen_modulu_ice_aktarmaz(self):
        bulunan = []
        for p in _proje_dosyalari():
            if p.suffix != ".py":
                continue
            rel = _goreli(p)
            with warnings.catch_warnings():
                # Eski dosyalardaki geçersiz kaçış dizileri (\T vb.) bu
                # taramanın konusu değil.
                warnings.simplefilter("ignore", SyntaxWarning)
                agac = ast.parse(p.read_text(encoding="utf-8"), filename=rel)
            for satir, modul in _ice_aktarilan_moduller(agac, rel):
                for olu in SILINEN_MODULLER:
                    if modul == olu or modul.startswith(olu + "."):
                        bulunan.append(f"{rel}:{satir}: {modul}")
        assert bulunan == [], bulunan

    def test_modul_listesi_yol_listesiyle_ayni(self):
        assert "core.book_translator_v2" in SILINEN_MODULLER
        assert "pdf_engine" in SILINEN_MODULLER
        assert len(SILINEN_MODULLER) == len(SILINEN_YOLLAR)


# ---------------------------------------------------------------------------
# (3) Kişisel sabit yol
# ---------------------------------------------------------------------------

# Yazarın ev klasörü; bu dosyada bitişik yazılmaz ki tarama kendini bulmasın.
_KISISEL_YOL = "/Users/" + "apple"


def test_kisisel_sabit_yol_yok():
    bulunan = []
    for p in _proje_dosyalari():
        if p.suffix not in _METIN_UZANTILARI:
            continue
        metin = p.read_text(encoding="utf-8", errors="replace")
        if _KISISEL_YOL in metin:
            satirlar = [i for i, s in enumerate(metin.splitlines(), 1)
                        if _KISISEL_YOL in s]
            bulunan.append(f"{_goreli(p)}: satır {satirlar}")
    assert bulunan == [], bulunan


class TestPdfExporterDenemesi:
    """exporters/pdf_exporter.py'nin geliştirici denemesi (``__main__``)
    eskiden yazarın masaüstündeki bir kitabı sabit yolla arıyordu; artık PDF
    yolunu komut satırından alır."""

    _BETIK = PROJE / "exporters" / "pdf_exporter.py"

    def test_yol_verilmezse_kullanim_basar(self, monkeypatch, capsys):
        monkeypatch.setattr(sys, "argv", ["pdf_exporter.py"])
        with pytest.raises(SystemExit) as cikis:
            runpy.run_path(str(self._BETIK), run_name="__main__")
        assert cikis.value.code == 2
        assert "Kullanım" in capsys.readouterr().err

    def test_verilen_pdfin_sayfasini_inceler(self, tmp_path, monkeypatch, capsys):
        fitz = pytest.importorskip("fitz")
        pdf = tmp_path / "deneme.pdf"
        belge = fitz.open()
        for metin in ("Birinci sayfa metni.", "İkinci sayfa metni."):
            belge.new_page().insert_text((72, 72), metin)
        belge.save(pdf)
        belge.close()
        monkeypatch.setattr(sys, "argv", ["pdf_exporter.py", str(pdf), "2", "9"])
        runpy.run_path(str(self._BETIK), run_name="__main__")
        yakalanan = capsys.readouterr()
        assert "SAYFA 2" in yakalanan.out and "SAYFA 1" not in yakalanan.out
        assert "Sayfa 9 yok" in yakalanan.err


def test_tarama_kapsami_canli_dosyalari_gorur():
    """Tarama boş dönerek yanlış yeşil vermesin: canlı girişler kapsamda,
    iç belgeler ve yerel klasörler kapsam dışında."""
    rels = {_goreli(p) for p in _proje_dosyalari()}
    for canli in ("translate_book.py", "1_cikar.py", "ui/runner.py",
                  "exporters/pdf_exporter.py", "tests/test_T1_g.py"):
        assert canli in rels, canli
    assert not any(r.startswith(("output/", "venv/", "docs/archive/"))
                   for r in rels)
    assert "TODO.md" not in rels and "KURALLAR.md" not in rels


# ---------------------------------------------------------------------------
# (4) Telifli dosya
# ---------------------------------------------------------------------------

def test_depoda_e_kitap_yok():
    bulunan = [_goreli(p) for p in _proje_dosyalari()
               if p.suffix.lower() in _E_KITAP_UZANTILARI]
    assert bulunan == [], bulunan


# ---------------------------------------------------------------------------
# (5) Açık anlık görüntüden dışlanan iç belgeler
# ---------------------------------------------------------------------------

def _gitattributes_satirlari() -> set[tuple[str, ...]]:
    yol = PROJE / ".gitattributes"
    assert yol.is_file(), ".gitattributes yok"
    return {tuple(s.split()) for s in yol.read_text(encoding="utf-8").splitlines()
            if s.strip() and not s.lstrip().startswith("#")}


@pytest.mark.parametrize("desen", IC_BELGE_DESENLERI)
def test_ic_belge_export_ignore(desen):
    assert (desen, "export-ignore") in _gitattributes_satirlari(), desen


def test_export_ignore_eslesmesi():
    assert _export_ignore_mu("docs/archive/PLAN.md")
    assert _export_ignore_mu("kutuphane/README.md")
    assert _export_ignore_mu("reports/AUDIT_20260421.md")
    assert _export_ignore_mu("TODO.md")
    assert not _export_ignore_mu("docs/KURULUM_VE_KULLANIM.md")
    assert not _export_ignore_mu("README.md")
    assert not _export_ignore_mu("core/TODO.md")


def test_cikti_ve_model_klasorleri_gitignore():
    satirlar = {s.strip() for s in
                (PROJE / ".gitignore").read_text(encoding="utf-8").splitlines()}
    assert "output/" in satirlar
    assert "models/" in satirlar
