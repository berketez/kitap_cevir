"""Sürüm betiğinin ağsız birim sınamaları (packaging/surum/).

Koşma (proje kökünden): ``./venv/bin/python -m pytest -q packaging/surum/test_surum.py``

Ağ, DMG ya da ölçüm koşusu gerektirmez: tekerlek seçimi, dışlama/bekçi, kilit
grupları, yeniden üretilebilir arşiv ve manifest kurgusunun şemaya (stdlib +
jsonschema) uyması sentetik girdilerle sınanır.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path
from types import SimpleNamespace

import pytest

BURASI = Path(__file__).resolve().parent
sys.path.insert(0, str(BURASI))

import onbellek as ob  # noqa: E402
import surum_ayarlari as SA  # noqa: E402
import surum_cikar as S  # noqa: E402
import tekerlek as T  # noqa: E402

DEPO = BURASI.parent.parent
PY = (3, 12)


def _w(ad: str, boyut: int = 1) -> dict:
    return {"filename": ad, "url": f"https://files.pythonhosted.org/x/{ad}", "size": boyut,
            "sha256": hashlib.sha256(ad.encode()).hexdigest()}


# ---------------------------------------------------------------------------
# Tekerlek seçimi
# ---------------------------------------------------------------------------
def test_macos_surumune_gore_ayri_tekerlek_ve_26_arti():
    dosyalar = [_w("mlx_metal-0.31.0-py3-none-macosx_14_0_arm64.whl"),
                _w("mlx_metal-0.31.0-py3-none-macosx_15_0_arm64.whl"),
                _w("mlx_metal-0.31.0-py3-none-macosx_26_0_arm64.whl")]
    secim = T.sec(dosyalar, PY, (14, 15, 26))
    assert {k: v["filename"].split("_")[-3] for k, v in secim.items()} == {
        14: "14", 15: "15", 26: "26"}
    kos = [(r["filename"], k) for r, k in T.kosullar(secim)]
    assert kos == [("mlx_metal-0.31.0-py3-none-macosx_14_0_arm64.whl", ["14"]),
                   ("mlx_metal-0.31.0-py3-none-macosx_15_0_arm64.whl", ["15"]),
                   ("mlx_metal-0.31.0-py3-none-macosx_26_0_arm64.whl", ["26+"])]


def test_ortak_tekerlek_kosulsuz_ve_14_15_birlesir():
    tek = [_w("numpy-2.4.2-cp312-cp312-macosx_14_0_arm64.whl")]
    assert [(r["filename"], k) for r, k in T.kosullar(T.sec(tek, PY, (14, 15, 26)))] == [
        ("numpy-2.4.2-cp312-cp312-macosx_14_0_arm64.whl", None)]
    iki = [_w("a-1-cp312-cp312-macosx_14_0_arm64.whl"), _w("a-1-cp312-cp312-macosx_26_0_arm64.whl")]
    assert [k for _, k in T.kosullar(T.sec(iki, PY, (14, 15, 26)))] == [["14", "15"], ["26+"]]


@pytest.mark.parametrize("dosyalar,beklenen", [
    # cp312-cp312 > abi3 > py3-none; platforma özgü > any
    (["p-1-py3-none-any.whl", "p-1-cp312-cp312-macosx_11_0_arm64.whl",
      "p-1-cp39-abi3-macosx_11_0_arm64.whl"], "p-1-cp312-cp312-macosx_11_0_arm64.whl"),
    (["p-1-py3-none-any.whl", "p-1-cp39-abi3-macosx_11_0_arm64.whl"],
     "p-1-cp39-abi3-macosx_11_0_arm64.whl"),
    # arm64 > universal2 (aynı sürüm); yüksek platform sürümü önce
    (["p-1-cp312-cp312-macosx_11_0_universal2.whl", "p-1-cp312-cp312-macosx_11_0_arm64.whl"],
     "p-1-cp312-cp312-macosx_11_0_arm64.whl"),
    (["p-1-cp312-cp312-macosx_11_0_arm64.whl", "p-1-cp312-cp312-macosx_14_0_arm64.whl"],
     "p-1-cp312-cp312-macosx_14_0_arm64.whl"),
    # yapı numarası (PEP 427): büyük olan
    (["torch-2.10.0-cp312-none-macosx_11_0_arm64.whl",
      "torch-2.10.0-2-cp312-none-macosx_11_0_arm64.whl"],
     "torch-2.10.0-2-cp312-none-macosx_11_0_arm64.whl"),
    # Intel, Linux, başka Python ve yeni macOS tekerleği seçilmez
    (["p-1-cp312-cp312-macosx_10_9_x86_64.whl", "p-1-cp312-cp312-manylinux_2_17_aarch64.whl",
      "p-1-cp313-cp313-macosx_11_0_arm64.whl", "p-1-cp312-cp312-macosx_15_0_arm64.whl",
      "p-1-py2.py3-none-any.whl"], "p-1-py2.py3-none-any.whl"),
])
def test_oncelik(dosyalar, beklenen):
    assert T.sec([_w(d) for d in dosyalar], PY, (14,))[14]["filename"] == beklenen


def test_kurulabilen_tekerlek_yoksa_hata():
    with pytest.raises(ValueError, match="macOS 14"):
        T.sec([_w("p-1-cp312-cp312-macosx_15_0_arm64.whl")], PY, (14, 15))


# ---------------------------------------------------------------------------
# Dışlama ve bekçi (açık yayına girmeyenler)
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("yol", [
    "data/test_data/ornek-en.epub", "docs/archive/PLAN.md", "kutuphane/README.md",
    "reports/AUDIT_20260421.md", "GELECEK_PLANI.md", "TODO.md", "KURALLAR.md",
    "output/x/EXPORT.tex", "models/hymt-7b-mlx/config.json", "tests/test_a.py",
    ".claude/settings.local.json", "dev_tools/ai_reviewer.py", "packaging/testler/test_x.py",
    "packaging/surum/surum_cikar.py", "CLAUDE.md", "core/CLAUDE.md",
    "latex_pipeline/sanitizer.py.orig", "a.orig", "x/__pycache__/y.pyc"])
def test_dislananlar(yol):
    assert S.dislanmis_mi(yol)


@pytest.mark.parametrize("yol", [
    "app.py", "1_cikar.py", "2_derle.py", "translate_book.py", "core/hardware.py",
    "packaging/yonetici/yonetici.py", "packaging/requirements/cekirdek.txt",
    "packaging/tex/isitma.tex", "packaging/tex/xelatex", "docs/KURULUM_VE_KULLANIM.md",
    "CEVIRI_TALIMATI.md", ".streamlit/config.toml", "LICENSE",
    "tools/pixel_diff.py"])
def test_kalanlar(yol):
    assert not S.dislanmis_mi(yol)


def test_bekci_berke_listesini_yakalar():
    adlar = ["app.py", "kutuphane/BUG_CORPUS.md", "TODO.md", "data/test_data/a.epub",
             "output/k/x.tex", "docs/archive/V.md", "reports/r.md", "models/m"]
    assert S.bekci_ihlalleri(adlar) == adlar[1:]


def test_arsiv_yeniden_uretilebilir_ve_bekci_durdurur(tmp_path):
    kaynak = tmp_path / "k"
    (kaynak / "packaging").mkdir(parents=True)
    (kaynak / "app.py").write_text("print('merhaba')\n", encoding="utf-8")
    (kaynak / "packaging" / "x.sh").write_text("#!/bin/sh\n", encoding="utf-8")
    (kaynak / "packaging" / "x.sh").chmod(0o755)
    a = S.arsiv_yaz(kaynak, tmp_path / "a.tar.gz", 1_700_000_000)
    b = S.arsiv_yaz(kaynak, tmp_path / "b.tar.gz", 1_700_000_000)
    assert a["sha256"] == b["sha256"] and a["dosya_sayisi"] == 2
    with tarfile.open(tmp_path / "a.tar.gz") as tar:
        uyeler = {u.name: u for u in tar.getmembers()}
    assert set(uyeler) == {"app.py", "packaging/x.sh"}      # içerik kökte (kabuk sözleşmesi)
    assert uyeler["packaging/x.sh"].mode == 0o755 and uyeler["app.py"].mode == 0o644
    assert {u.uid for u in uyeler.values()} == {0}
    # Bekçi, dışlama listesi bozulsa bile (burada boşaltıldı) Berke listesini durdurur.
    (kaynak / "TODO.md").write_text("x", encoding="utf-8")
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(SA, "DISLANANLAR", ())
        with pytest.raises(S.SurumHatasi, match="TODO.md"):
            S.arsiv_yaz(kaynak, tmp_path / "c.tar.gz", 1)


def _bytecode_agaci(kok: Path) -> None:
    for goreli in ("core/__pycache__/hardware.cpython-312.pyc", "__pycache__/a.cpython-312.pyc",
                   "packaging/yonetici/adimlar/__pycache__/ortak.cpython-312.pyc", "ui/eski.pyc"):
        (kok / goreli).parent.mkdir(parents=True, exist_ok=True)
        (kok / goreli).write_bytes(b"\x00derleme yolu: /private/tmp/.../kaynak/x.py")


def test_arsiv_dislamayi_yeniden_uygular_bytecode_girmez(tmp_path):
    """K9 E1: temiz kaynaktan içe aktarma __pycache__ yazıyordu ve arsiv_yaz rglob ile her
    şeyi alıyordu (19 .pyc, içlerinde sürüm makinesinin yolu). Dışlama arşivde yeniden
    uygulanır; Berke listesindeki bir dosya da kaynakta belirse arşive girmez."""
    kaynak = tmp_path / "k"
    kaynak.mkdir()
    (kaynak / "app.py").write_text("print('merhaba')\n", encoding="utf-8")
    _bytecode_agaci(kaynak)
    (kaynak / ".DS_Store").write_bytes(b"x")
    (kaynak / "TODO.md").write_text("x", encoding="utf-8")
    sonuc = S.arsiv_yaz(kaynak, tmp_path / "a.tar.gz", 1_700_000_000)
    with tarfile.open(tmp_path / "a.tar.gz") as tar:
        assert tar.getnames() == ["app.py"]
    assert sonuc["dosya_sayisi"] == 1


def test_bekci_bytecode_yakalar(tmp_path):
    adlar = ["app.py", "core/__pycache__/hardware.cpython-312.pyc", "__pycache__/a.pyc",
             "ui/eski.pyc", "x/__pycache__/y.txt"]
    assert S.bekci_ihlalleri(adlar) == adlar[1:]
    kaynak = tmp_path / "k"
    kaynak.mkdir()
    (kaynak / "app.py").write_text("x", encoding="utf-8")
    _bytecode_agaci(kaynak)
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(SA, "DISLANANLAR", ())          # dışlama listesi bozulursa bekçi durdurur
        with pytest.raises(S.SurumHatasi, match="__pycache__"):
            S.arsiv_yaz(kaynak, tmp_path / "b.tar.gz", 1)


def test_temiz_kaynaktan_ice_aktarma_bytecode_yazmaz(tmp_path):
    """Kök neden (K9 E1): yonetici_modulleri() ve branding_oku() temiz kaynaktan içe aktarıp
    __pycache__ yazıyordu. Ayrı yorumlayıcıda (sys.modules önbelleği olmadan) sınanır;
    PYTHONDONTWRITEBYTECODE ortamdan çıkarılır ki sınama kendiliğinden geçmesin."""
    kaynak = tmp_path / "kaynak"
    yok_say = shutil.ignore_patterns("__pycache__", "*.pyc")
    for goreli in ("packaging/yonetici", "core", "config", "ui"):
        shutil.copytree(DEPO / goreli, kaynak / goreli, ignore=yok_say)
    betik = ("import sys\n"
             f"sys.path.insert(0, {str(BURASI)!r})\n"
             "from pathlib import Path\n"
             "import surum_cikar as S\n"
             f"k = Path({str(kaynak)!r})\n"
             "S.yonetici_modulleri(k)\n"
             "print(S.branding_oku(k)['APP_NAME'])\n")
    ortam = {a: d for a, d in os.environ.items()
             if a not in ("PYTHONDONTWRITEBYTECODE", "PYTHONPYCACHEPREFIX")}
    sonuc = subprocess.run([sys.executable, "-c", betik], capture_output=True, text=True,
                           env=ortam, timeout=120)
    assert sonuc.returncode == 0, sonuc.stderr[-2000:]
    assert sonuc.stdout.strip().splitlines()[-1] == "Kitapçevir"
    yazilan = sorted(str(p.relative_to(kaynak)) for p in kaynak.rglob("*")
                     if p.name == "__pycache__" or p.suffix == ".pyc")
    assert yazilan == []


# ---------------------------------------------------------------------------
# Kilit grupları
# ---------------------------------------------------------------------------
def _p(ad, surum):
    return SimpleNamespace(ad=ad, normal=T.normal_ad(ad), surum=surum, ozetler=("0" * 64,))


def test_grup_paketleri_ortak_paket_ilk_gruba_ve_bagimlilik():
    kilitler = {"cekirdek": [_p("numpy", "2"), _p("streamlit", "1")],
                "mlx": [_p("numpy", "2"), _p("mlx", "0.31")],
                "pdf": [_p("numpy", "2"), _p("mlx", "0.31"), _p("torch", "2.10")],
                "gguf": [_p("numpy", "2"), _p("llama-cpp-python", "0.3.35")]}
    yeni, bagimli = S.grup_paketleri(kilitler)
    assert {g: [p.normal for p in v] for g, v in yeni.items()} == {
        "cekirdek": ["numpy", "streamlit"], "mlx": ["mlx"], "pdf": ["torch"],
        "gguf": ["llama-cpp-python"]}
    assert bagimli == {"cekirdek": [], "mlx": ["cekirdek"], "pdf": ["cekirdek", "mlx"],
                       "gguf": ["cekirdek"]}


def test_grup_paketleri_surum_celiskisi_durdurur():
    kilitler = {"cekirdek": [_p("numpy", "2")], "mlx": [_p("numpy", "1")], "pdf": [], "gguf": []}
    with pytest.raises(S.SurumHatasi, match="sürüm çelişkisi"):
        S.grup_paketleri(kilitler)


def test_gercek_kilitlerde_celiski_yok():
    m = S.yonetici_modulleri(DEPO)
    yeni, bagimli = S.grup_paketleri(S.kilitleri_oku(m, DEPO))
    assert sum(len(v) for v in yeni.values()) > 0
    assert bagimli["cekirdek"] == []


def test_ozet_dosyasindan():
    metin = f"{'a' * 64}  x.tar.gz\n{'b' * 64}  *y.tar.gz\n"
    assert S.ozet_dosyasindan(metin, "y.tar.gz") == "b" * 64
    with pytest.raises(S.SurumHatasi):
        S.ozet_dosyasindan(metin, "z")


# ---------------------------------------------------------------------------
# Manifest kurgusu: şemaya ve anlamsal kurallara uyar (sentetik ölçümlerle)
# ---------------------------------------------------------------------------
def _kayit(ad: str, bayt: int) -> ob.Kayit:
    return ob.Kayit(url=f"https://example.org/{ad}", yol=Path(ad), bayt=bayt,
                    sha256=hashlib.sha256(ad.encode()).hexdigest())


def _oge(kimlik, bayt, hedef, **ek):
    o = {"kimlik": kimlik, "url": f"https://files.pythonhosted.org/{kimlik}", "bayt": bayt,
         "sha256": hashlib.sha256(kimlik.encode()).hexdigest(), "hedef": hedef}
    o.update(ek)
    return o


def _sentetik_manifest(m, disk=None, sayac=352):
    tek = lambda ad, **ek: _oge(ad, 1000, f"indirilen/tekerlek/{ad}", tur="tekerlek", **ek)  # noqa
    tekerlekler = {
        "cekirdek": [tek("streamlit-1.55.0-py3-none-any.whl")],
        "mlx": [tek("mlx_metal-0.31.0-py3-none-macosx_14_0_arm64.whl", kosul={"macos": ["14"]}),
                tek("mlx_metal-0.31.0-py3-none-macosx_15_0_arm64.whl", kosul={"macos": ["15"]}),
                tek("mlx_metal-0.31.0-py3-none-macosx_26_0_arm64.whl",
                    kosul={"macos": ["26+"]})],
        "pdf": [tek("torch-2.10.0-cp312-none-macosx_11_0_arm64.whl")],
        "gguf": [tek("llama_cpp_python-0.3.35-py3-none-macosx_14_0_arm64.whl")],
    }
    datalab = [_oge("datalab-layout-2025_09_23-model.safetensors", 5000,
                    "modeller/datalab/layout/2025_09_23/model.safetensors", tur="dosya",
                    yeniden_kullan=["~/Library/Caches/datalab/models/layout/2025_09_23"])]
    hymt_ogeler = [_oge("hymt-model.safetensors", 7000, f"{SA.HYMT_KLASORU}/model.safetensors",
                        tur="dosya"),
                   _oge("hymt-lisans", 16, "indirilen/hymt/License.txt", tur="dosya")]
    hymt = {"ogeler": hymt_ogeler, "config": {"num_hidden_layers": 32},
            "agirlik_bayt": 7000, "toplam_bayt": 7016,
            "kv": {"katman": 32, "kv_basligi": 8, "head_dim": 128, "oge_bayt": 2},
            "lisans_sha256": "d" * 64, "lisans_gorunen": "https://huggingface.co/t/x/blob/r/L.txt"}
    return S.manifest_kur(
        surum="1.0.0", kaynak={"kisa": "4567c85", "kirli": True}, sabitler={"MIN_MACOS": "14.0"},
        m=m, olcum_bilgisi={"tarih": "2026-10-05", "makine": "Apple M4 Max", "macos": "26.7.1",
                            "yontem": "sınama"},
        marka={"model_tirnakli": "“Yerel model ile çevir”", "MODEL_BETA_LABEL": "Beta"},
        pbs=_kayit(SA.PBS_ARSIV_ADI, 25), uv=_kayit("uv.tar.gz", 17), tectonic=_kayit("t.tgz", 21),
        mactex={"bayt": 6_865_013_189, "sha256": "e" * 64, "sha512": "f" * 128,
                "son_degisiklik": "Tue, 24 Mar 2026 18:03:18 GMT"},
        tekerlekler=tekerlekler, datalab={"ogeler": datalab, "lisans_sha256": "a" * 64},
        hymt=hymt, disk=disk or {k: 10 for k in ("cekirdek", "mlx", "pdf", "tex-hafif", "gguf")},
        sayac=sayac, olcum_notu="sınama", kilit_surumleri={
            "marker-pdf": "1.10.2", "mlx": "0.31.0", "llama-cpp-python": "0.3.35"},
        bagimli={"cekirdek": [], "mlx": ["cekirdek"], "pdf": ["cekirdek", "mlx"],
                 "gguf": ["cekirdek"]})


def test_manifest_kurgusu_sema_ve_anlamsal_kurallar():
    m = S.yonetici_modulleri(DEPO)
    veri = _sentetik_manifest(m)
    sonuc = S.manifest_dogrula(m, veri, DEPO)
    assert sonuc["stdlib"] == "geçti"
    mlx = next(b for b in veri["bilesenler"] if b["kimlik"] == "mlx")
    assert set(mlx["boyut"]["indirme_bayt"]) == {"14", "15", "26"}   # koşullu öğe: macOS'a göre
    assert m["manifest"].bayt_sec(mlx["boyut"]["indirme_bayt"], "27") == 1000   # 27 → "26" ölçümü
    tex = next(b for b in veri["bilesenler"] if b["kimlik"] == "tex-hafif")
    assert tex["sayac_beklenen"] == {"birim": "dosya", "n": 352}
    assert "hymt" not in [b for b in veri["bilesenler"] if b.get("zorunluluk") == "zorunlu"]
    # Depo adresi manifestte yok (tek yer ayarlar.py), bölge alanı yok (Berke kararı)
    metin = str(veri)
    assert "bolge" not in metin


def test_manifest_sayac_yoksa_alan_yazilmaz():
    m = S.yonetici_modulleri(DEPO)
    veri = _sentetik_manifest(m, sayac=None)
    tex = next(b for b in veri["bilesenler"] if b["kimlik"] == "tex-hafif")
    assert "sayac_beklenen" not in tex
    assert S.manifest_dogrula(m, veri, DEPO)["stdlib"] == "geçti"


def test_manifest_dogrulama_bozuk_manifesti_reddeder():
    m = S.yonetici_modulleri(DEPO)
    veri = _sentetik_manifest(m)
    veri["bilesenler"][0]["boyut"]["indirme_bayt"]["hepsi"] += 1      # öğe toplamıyla uyuşmaz
    with pytest.raises(S.SurumHatasi, match="indirme_bayt"):
        S.manifest_dogrula(m, veri, DEPO)


# ---------------------------------------------------------------------------
# MacTeX özetleri: TUG sayfası (yansıdaki .sha512 24 Mart 2026'dan beri bayat)
# ---------------------------------------------------------------------------
class _SahteOnbellek:
    def __init__(self, yanitlar):
        self.yanitlar = yanitlar

    def oku(self, url, en_cok=0):
        if url not in self.yanitlar:
            raise ob.IndirmeHatasi(f"GET {url}: HTTP 404")
        return self.yanitlar[url].encode()


def test_mactex_ozetleri_tug_sayfasi_ve_yansi():
    tug = "a" * 128
    sayfa = f"<p><center>The Sha512 sum is <b>\n{tug}\n \n</b></center>"
    on = _SahteOnbellek({SA.MACTEX_TUG_SAYFASI: sayfa,
                         SA.MACTEX_SHA512_URL: "b" * 128 + "  MacTeX.pkg\n"})
    assert S.mactex_ozetleri(on) == {"tug": tug, "yansi": "b" * 128}
    assert S.mactex_ozetleri(_SahteOnbellek({})) == {}


def test_manifestte_mactex_sha512_url_yok_sha256_sabit():
    m = S.yonetici_modulleri(DEPO)
    tam = next(b for b in _sentetik_manifest(m)["bilesenler"] if b["kimlik"] == "tex-tam")
    assert "sha512_url" not in tam["ogeler"][0] and tam["ogeler"][0]["sha256"] == "e" * 64


# ---------------------------------------------------------------------------
# K9 E2: gömülü üçüncü taraf ikililerin lisans DOSYALARI (özet değil; KARAR 34)
# ---------------------------------------------------------------------------
def _tar_gz(yol: Path, uyeler: dict) -> Path:
    import io
    with tarfile.open(yol, "w:gz") as tar:
        for ad, icerik in uyeler.items():
            bilgi = tarfile.TarInfo(ad)
            bilgi.size = len(icerik)
            tar.addfile(bilgi, io.BytesIO(icerik))
    return yol


_HACL_C = ("/* MIT License\n *\n * Copyright (c) 2016-2022 Örnek Kurum\n *\n"
           " * Permission is hereby granted, free of charge, to any person.\n"
           " * THE SOFTWARE IS PROVIDED \"AS IS\".\n */\n\n\n#include \"x.h\"\n")


def test_hacl_lisansi_yorum_isaretsiz():
    metin = S.hacl_lisansi(_HACL_C)
    assert metin.splitlines()[0] == "MIT License"
    assert "Copyright (c) 2016-2022 Örnek Kurum" in metin and "Permission is hereby granted" in metin
    assert "/*" not in metin and "*/" not in metin and "\n *" not in metin
    with pytest.raises(S.SurumHatasi):
        S.hacl_lisansi("#include \"x.h\"\n")


def _pbs_install_only(tmp_path: Path, tk: bool = True) -> Path:
    uyeler = {"python/lib/itcl4.3.8/pkgIndex.tcl": b"x",
              "python/lib/thread3.0.6/pkgIndex.tcl": b"x",
              "python/lib/python3.12/LICENSE.txt": b"PSF"}
    if tk:
        uyeler["python/lib/tk9.0/pkgIndex.tcl"] = (
            b"if {![package vsatisfies [package provide Tcl] 9.0]} return\n"
            b"    package ifneeded tk 9.0.4 [list load x]\n")
    return _tar_gz(tmp_path / "pbs.tar.gz", uyeler)


def test_tcl_eklenti_surumleri(tmp_path):
    assert S.tcl_eklenti_surumleri(_pbs_install_only(tmp_path)) == {
        "tk": "9.0.4", "itcl": "4.3.8", "thread": "3.0.6"}
    (tmp_path / "tksiz").mkdir()
    with pytest.raises(S.SurumHatasi, match="tk"):
        S.tcl_eklenti_surumleri(_pbs_install_only(tmp_path / "tksiz", tk=False))


@pytest.mark.skipif(shutil.which("zstd") is None, reason="zstd yok (sürüm makinesinde gerekir)")
def test_pbs_full_lisanslari_zstd_akisindan(tmp_path):
    tar_yolu = tmp_path / "full.tar"
    import io
    with tarfile.open(tar_yolu, "w") as tar:
        for ad, icerik in {"python/PYTHON.json": b"{}", "python/licenses/LICENSE.openssl-3.txt":
                           b"Apache", "python/licenses/LICENSE.tcl.txt": b"Tcl",
                           "python/licenses/alt/x.txt": b"alt"}.items():
            bilgi = tarfile.TarInfo(ad)
            bilgi.size = len(icerik)
            tar.addfile(bilgi, io.BytesIO(icerik))
    subprocess.run([shutil.which("zstd"), "-q", str(tar_yolu), "-o", str(tmp_path / "full.tar.zst")],
                   check=True)
    assert S.pbs_full_lisanslari(tmp_path / "full.tar.zst", shutil.which("zstd")) == {
        "LICENSE.openssl-3.txt": b"Apache", "LICENSE.tcl.txt": b"Tcl"}
    with pytest.raises(S.SurumHatasi):
        S.pbs_full_lisanslari(tar_yolu, shutil.which("zstd"))        # zstd'li değil


class _SahteLisansOnbellegi:
    """Sabit etiketli lisans metinlerini yerel dosyadan veren önbellek (ağsız)."""

    def __init__(self, kok: Path):
        self.kok = kok
        self.istenen = []

    def dosya(self, url, *, ad=None, sha256=None, bayt=None, sha512=False, basla_denetle=True):
        self.istenen.append(url)
        icerik = (_HACL_C if url.endswith(".c") else f"lisans metni: {url}\n").encode()
        yol = self.kok / hashlib.sha256(url.encode()).hexdigest()[:12] / (ad or "x")
        yol.parent.mkdir(parents=True, exist_ok=True)
        yol.write_bytes(icerik)
        return ob.Kayit(url=url, yol=yol, bayt=len(icerik), sha256=hashlib.sha256(icerik).hexdigest())


def _ucuncu_taraf(tmp_path, monkeypatch):
    monkeypatch.setattr(S, "pbs_full_lisanslari", lambda arsiv, zstd: {
        "LICENSE.cpython.txt": b"PSF + Incorporated", "LICENSE.openssl-3.txt": b"Apache",
        "LICENSE.mpdecimal.txt": b"BSD-2", "LICENSE.bzip2.txt": b"bzip2", "LICENSE.expat.txt": b"MIT",
        "LICENSE.tcl.txt": b"Tcl"})
    sdist = _tar_gz(tmp_path / "llama.tar.gz", {
        "llama_cpp_python-0.3.35/LICENSE.md": b"MIT llama-cpp-python",
        "llama_cpp_python-0.3.35/vendor/llama.cpp/LICENSE": b"MIT ggml",
        "llama_cpp_python-0.3.35/vendor/llama.cpp/licenses/LICENSE-jsonhpp": b"MIT json",
        "llama_cpp_python-0.3.35/vendor/llama.cpp/vendor/cpp-httplib/LICENSE": b"MIT httplib"})
    on = _SahteLisansOnbellegi(tmp_path / "onbellek")
    ucuncu = S.ucuncu_taraf_lisanslari(
        on, pbs=SimpleNamespace(yol=_pbs_install_only(tmp_path)), pbs_full=SimpleNamespace(yol=None),
        gguf_sdist=sdist, gguf_surum="0.3.35", zstd="zstd")
    return ucuncu, on


def test_ucuncu_taraf_lisanslari_gruplari(tmp_path, monkeypatch):
    ucuncu, on = _ucuncu_taraf(tmp_path, monkeypatch)
    assert set(ucuncu) == {"python", "uv", "tectonic", "llama-cpp-python"}
    py = ucuncu["python"]["dosyalar"]
    assert {"LICENSE.openssl-3.txt", "LICENSE.mpdecimal.txt", "LICENSE.bzip2.txt", "LICENSE.expat.txt",
            "LICENSE.tcl.txt", "LICENSE.tk.txt", "LICENSE.itcl.txt", "LICENSE.thread.txt",
            "LICENSE.hacl-star.txt", "LICENSE.cpython.txt"} <= set(py)
    assert py["LICENSE.hacl-star.txt"].decode().startswith("MIT License")
    assert set(ucuncu["uv"]["dosyalar"]) == {"LICENSE-MIT", "LICENSE-APACHE"}
    assert set(ucuncu["tectonic"]["dosyalar"]) == {"LICENSE"}
    assert ucuncu["llama-cpp-python"]["dosyalar"]["LICENSE.llama.cpp.txt"] == b"MIT ggml"
    # Sürüm etiketleri arşivden okunan sürümlerle kurulur
    assert any("tcltk/tk/core-9-0-4/" in u for u in on.istenen)
    assert any("tcltk/itcl/itcl-4-3-8/" in u for u in on.istenen)
    assert any("tcltk/thread/thread-3-0-6/" in u for u in on.istenen)
    assert any(f"cpython/v{SA.PBS_PYTHON}/Modules/_hacl/" in u for u in on.istenen)
    assert any(f"astral-sh/uv/{SA.UV_SURUMU}/LICENSE-APACHE" in u for u in on.istenen)


def test_lisans_klasoru_ve_00_oku_gercekle_ayni(tmp_path, monkeypatch):
    """K9 E2/Y2: 00_OKU.txt'deki her dosya LICENSES/'te var, LICENSES/'teki her dosya
    00_OKU'da anılıyor; olmayan şey iddia edilmiyor (paket lisanslarının köke yazıldığı,
    tek python/LICENSE.txt'nin iç kitaplıkları kapsadığı gibi)."""
    ucuncu, _ = _ucuncu_taraf(tmp_path, monkeypatch)
    m = S.yonetici_modulleri(DEPO)
    veri = _sentetik_manifest(m)
    kaynak = tmp_path / "kaynak"
    kaynak.mkdir()
    (kaynak / "LICENSE").write_text("GPL-3.0", encoding="utf-8")
    hedef = tmp_path / "LICENSES"
    on = _SahteLisansOnbellegi(tmp_path / "onbellek2")
    datalab = on.dosya("https://ornek.invalid/MODEL_LICENSE", ad="MODEL_LICENSE")
    tencent = on.dosya("https://ornek.invalid/License.txt", ad="License.txt")
    S.lisanslari_hazirla(hedef, on=on, kaynak={"dizin": kaynak, "commit": "a" * 40, "kirli": False},
                         m=m, datalab_lisans=datalab, tencent_lisans=tencent,
                         kilit_surumleri={"pymupdf": "1.26.0"}, surum="1.0.0", manifest_veri=veri,
                         ucuncu_taraf=ucuncu)
    dosyalar = sorted(str(p.relative_to(hedef)) for p in hedef.rglob("*") if p.is_file())
    oku = (hedef / "00_OKU.txt").read_text(encoding="utf-8")
    for d in dosyalar:
        if d != "00_OKU.txt":
            assert d in oku, f"00_OKU'da anılmıyor: {d}"
    import re
    anilanlar = re.findall(r"(?<![\w/])((?:python|uv|tectonic|llama-cpp-python|datalab|tencent-hy)"
                           r"/[\w.+-]*[\w+-])", oku)      # cümle sonu noktası yola girmez
    assert anilanlar
    for anilan in anilanlar:
        assert (hedef / anilan).is_file(), f"olmayan dosya: {anilan}"
    assert "python/LICENSE.txt" not in oku
    assert "kurulum kökündeki LICENSES/ klasörüne yazılır" not in oku
    assert ".dist-info" in oku                     # paket lisanslarının gerçek yeri
    assert (hedef / "python" / "LICENSE.hacl-star.txt").is_file()
    assert not (hedef / "python" / "LICENSE.txt").exists()


# ---------------------------------------------------------------------------
# K9 O4: DMG'de ilk açılış yönergesi
# ---------------------------------------------------------------------------
def test_dmg_sahnesi_ilk_acilis_metni(tmp_path):
    app = tmp_path / SA.PAKET_ADI
    (app / "Contents").mkdir(parents=True)
    (app / "Contents" / "Info.plist").write_text("x", encoding="utf-8")
    metin = S.ilk_acilis_metni("Kitapçevir", "https://github.com/ornek/depo")
    for parca in ("sağ tık", "Aç", "Sistem Ayarları", "Gizlilik ve Güvenlik", "Yine de Aç",
                  "https://github.com/ornek/depo"):
        assert parca in metin, parca
    sahne = tmp_path / "sahne"
    S.dmg_sahnesi_hazirla(sahne, app, metin)
    assert sorted(p.name for p in sahne.iterdir()) == sorted(
        [SA.PAKET_ADI, "Applications", SA.DMG_ILK_ACILIS_ADI])
    assert (sahne / "Applications").resolve() == Path("/Applications")
    ham = (sahne / SA.DMG_ILK_ACILIS_ADI).read_bytes()
    assert ham.startswith(b"\xef\xbb\xbf") and ham.decode("utf-8-sig") == metin


def test_surum_notunda_ilk_acilis_iki_yolu():
    satirlar = S.ILK_ACILIS_NOTU
    assert "sağ tık" in satirlar and "Yine de Aç" in satirlar and SA.DMG_ILK_ACILIS_ADI in satirlar


def test_export_ignore_desenleri_uygulama_arsivinde_de_dislanir():
    """K9 O3: açık anlık görüntüden (git archive, .gitattributes export-ignore) çıkarılan iç
    belgeler, çalışma ağacından üretilen uygulama arşivine de girmemeli (DISLANANLAR)."""
    desenler = [satir.split()[0] for satir in
                (DEPO / ".gitattributes").read_text(encoding="utf-8").splitlines()
                if satir.strip() and not satir.lstrip().startswith("#")
                and "export-ignore" in satir.split()[1:]]
    assert desenler
    for desen in desenler:
        yol = desen.lstrip("/")
        ornek = yol + "ornek.md" if yol.endswith("/") else yol.replace("*", "ornek")
        assert S.dislanmis_mi(ornek), f"{desen} → {ornek} DISLANANLAR'da değil"
