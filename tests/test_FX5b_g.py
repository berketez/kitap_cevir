"""FX5b (5 Ekim 2026): açık yayın temizliği (KARAR 26, 34, 35).

Açık depo geçmişsiz bir anlık görüntüyle (``git archive``) kurulur;
``.gitattributes`` içindeki ``export-ignore`` desenleri iç belgeleri dışarıda
bırakır. Bu testler o anlık görüntüye girecek dosyalarda şunları sınar:

  (1) Kişisel bilgi yok: Tailscale aralığından (100.64.0.0/10) adres, gerçek
      e-posta adresi, makine adı.
  (2) Telifli kitap kalıntısı yok: kitap adları, yazarlar ve tek bir kitaba
      özgü terimler. Bu ifadeler test dosyasında düz metin olarak TUTULMAZ
      (yalnız SHA-256 özetlerinin ilk 16 hanesi). Sözlüklerde (glossary/)
      tek bir kitaba özgü giriş yok; genel teknik terimler kalır.
  (3) Açık yayına uygun olmayan iç belgeler export-ignore; açık belgeler iç
      belgeye bağlantı vermez, iç sürece (karar numarası, oturum klasörü,
      ajan adı) gönderme yapmaz.
  (4) Belgelerdeki depo adresi açık depodur; kendi yazdığımız lisans
      özetleri yok (KARAR 34: resmî lisans metinlerine bağlantı verilir).

Testler git, model, ağ ya da output/ gerektirmez; açık anlık görüntüde
(``git archive`` çıktısı, .git yok) de koşar.
"""
from __future__ import annotations

import fnmatch
import hashlib
import ipaddress
import json
import re
from functools import lru_cache
from pathlib import Path

import pytest

PROJE = Path(__file__).resolve().parent.parent

# Açık depo (KARAR 35: Berke'nin açacağı yeni, geçmişsiz depo).
ACIK_DEPO = "https://github.com/berketez/kitap_cevir"

# Depoya hiç girmeyen yerel klasörler/dosyalar (.gitignore) — taranmaz.
# kutuphane/: izlenen içeriği yalnız iç belgeler (export-ignore), alt
# klasörleri .gitignore'da (yerel kitap çalışmaları).
_YEREL_KLASORLER = {
    "venv", "output", "models", "cevrilenkitaplar", "kutuphane", "__pycache__",
    ".git", ".pytest_cache", ".playwright-mcp",
}
_YEREL_YOLLAR = {
    ".claude-project", "data/dictionary", "config/mt_selection.json",
    "config/mt_sources.json", "config/mt_engines.json",
}
_METIN_UZANTILARI = {
    ".py", ".sh", ".swift", ".js", ".html", ".css", ".tex", ".toml", ".ini",
    ".cfg", ".json", ".md", ".txt", ".yml", ".yaml", ".sablon", ".plist",
}
_METIN_ADLARI = {"LICENSE", ".gitignore", ".gitattributes"}

_SOZCUK = re.compile(r"\w+", re.UNICODE)


# ---------------------------------------------------------------------------
# Açık anlık görüntü: .gitattributes export-ignore (git'siz okunur)
# ---------------------------------------------------------------------------

def _export_ignore_desenleri() -> list[str]:
    desenler = []
    for satir in (PROJE / ".gitattributes").read_text(encoding="utf-8").splitlines():
        alan = satir.split()
        if alan and not alan[0].startswith("#") and "export-ignore" in alan[1:]:
            desenler.append(alan[0])
    return desenler


def _desen_eslesir(desen: str, rel: str) -> bool:
    """gitattributes deseni (git'in kuralları; ``**`` kullanılmıyor).

    Klasör yolları sonda "/" ile verilir (``git archive`` klasörü de
    denetler; "/" ile biten desen yalnız klasöre uyar, altındaki her şey
    dışarıda kalır)."""
    capali = desen.startswith("/")
    d = desen.lstrip("/")
    if d.endswith("/"):
        d = d.rstrip("/")
        klasorler = rel.rstrip("/").split("/")
        if not rel.endswith("/"):
            klasorler = klasorler[:-1]
        if capali or "/" in d:
            n = d.count("/") + 1
            return (len(klasorler) >= n
                    and fnmatch.fnmatchcase("/".join(klasorler[:n]), d))
        return any(fnmatch.fnmatchcase(k, d) for k in klasorler)
    if rel.endswith("/"):
        return False
    if not capali and "/" not in d:
        return fnmatch.fnmatchcase(rel.rsplit("/", 1)[-1], d)
    return rel.count("/") == d.count("/") and fnmatch.fnmatchcase(rel, d)


def _export_ignore_mu(rel: str) -> bool:
    return any(_desen_eslesir(d, rel) for d in _export_ignore_desenleri())


@lru_cache(maxsize=1)
def _acik_dosyalar() -> tuple[Path, ...]:
    out = []
    yigin = [PROJE]
    while yigin:
        klasor = yigin.pop()
        for p in sorted(klasor.iterdir()):
            rel = p.relative_to(PROJE).as_posix()
            if rel in _YEREL_YOLLAR:
                continue
            if p.is_dir():
                if p.name not in _YEREL_KLASORLER and not _export_ignore_mu(rel + "/"):
                    yigin.append(p)
            elif not _export_ignore_mu(rel):
                out.append(p)
    return tuple(out)


def _metin_dosyalari():
    for p in _acik_dosyalar():
        if p.suffix in _METIN_UZANTILARI or p.name in _METIN_ADLARI:
            yield p


def _goreli(p: Path) -> str:
    return p.relative_to(PROJE).as_posix()


def _oku(p: Path) -> str:
    return p.read_text(encoding="utf-8", errors="replace")


def test_desen_okuyucu_git_kurallarina_uyar():
    assert _desen_eslesir("/docs/archive/", "docs/archive/PLAN.md")
    assert _desen_eslesir("/docs/archive/", "docs/archive/")
    assert not _desen_eslesir("/docs/archive/", "docs/archive")      # dosya, klasör değil
    assert not _desen_eslesir("/docs/archive/", "x/docs/archive/a.md")
    assert _desen_eslesir("/kutuphane/*.md", "kutuphane/README.md")
    assert not _desen_eslesir("/kutuphane/*.md", "kutuphane/a/b.md")
    assert _desen_eslesir("CLAUDE.md", "core/CLAUDE.md")             # çapasız ad
    assert not _desen_eslesir("/CLAUDE.md", "core/CLAUDE.md")


def test_tarama_kapsami():
    """Tarama boş dönüp yanlış yeşil vermesin."""
    rels = {_goreli(p) for p in _metin_dosyalari()}
    for canli in ("README.md", "CEVIRI_TALIMATI.md", "glossary/curated.json",
                  "core/ner_protector.py", "tests/test_FX5b_g.py", "LICENSE"):
        assert canli in rels, canli
    assert not any(r.startswith(("output/", "venv/", "docs/archive/", "kutuphane/"))
                   for r in rels)


# ---------------------------------------------------------------------------
# (1) Kişisel bilgi
# ---------------------------------------------------------------------------

# Tailscale adresleri 100.64.0.0/10 içinden rastgele dağıtılır. Belge ve
# testlerde örnek adres olarak yalnız 100.64.0.0/24 kullanılır.
_CGNAT = ipaddress.ip_network("100.64.0.0/10")
_ORNEK_AG = ipaddress.ip_network("100.64.0.0/24")
_IPV4 = re.compile(r"(?<![\d.])(\d{1,3}(?:\.\d{1,3}){3})(?!\d)(?!\.\d)")


def test_tailscale_adresi_yok():
    bulunan = []
    for p in _metin_dosyalari():
        for no, satir in enumerate(_oku(p).splitlines(), 1):
            for m in _IPV4.finditer(satir):
                try:
                    ip = ipaddress.ip_address(m.group(1))
                except ValueError:
                    continue
                if ip in _CGNAT and ip not in _ORNEK_AG:
                    bulunan.append(f"{_goreli(p)}:{no}: {m.group(1)}")
    assert bulunan == [], bulunan


_EPOSTA = re.compile(
    r"(?<![\w.%+-])[\w.%+-]+@([A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,})")
# Ayrılmış örnek alanlar (RFC 2606) ve genel yer tutucu.
_ORNEK_ALANLAR = {"example.com", "example.org", "example.net"}
_ORNEK_EPOSTALAR = {"user@domain.com"}


def test_gercek_eposta_adresi_yok():
    bulunan = []
    for p in _metin_dosyalari():
        for no, satir in enumerate(_oku(p).splitlines(), 1):
            for m in _EPOSTA.finditer(satir):
                alan = m.group(1).lower()
                if (alan.endswith(".example") or alan in _ORNEK_ALANLAR
                        or m.group(0).lower() in _ORNEK_EPOSTALAR
                        or satir[:m.start()].endswith("://")):   # URL kullanıcı bilgisi
                    continue
                bulunan.append(f"{_goreli(p)}:{no}: {m.group(0)}")
    assert bulunan == [], bulunan


# ---------------------------------------------------------------------------
# (2) Telifli kitap kalıntısı ve kişi/makine adları (özetle)
# ---------------------------------------------------------------------------

# SHA-256(ifade)[:16] -> (sözcük sayısı, ilk sözcüğün özeti, açıklama).
# İfade: küçük harf, \w+ sözcükleri tek boşlukla. Düz metin listesi özel
# depodadır (iç denetim klasöründe).
_YASAK_IFADELER = {
    "de52fdda936afbda": (2, "fda503e47e82c440", "kitap adı (dizi)"),
    "f4db018dad0c24a6": (2, "bbd334ffa6772a83", "yazar"),
    "92b487a5ec8d5c38": (1, "92b487a5ec8d5c38", "yazar soyadı"),
    "c7515ef6032800ce": (2, "afa6e9ab3ac6574d", "kitap adı"),
    "35568d7b4d7c679d": (4, "64a3f18ab4fab299", "kitap adı"),
    "ad89d6a9c205d02f": (3, "c58dd0a3760008c9", "kitap adı"),
    "093ba5eb43042c18": (4, "fa64846fda90a4e6", "kitap adı"),
    "49ef2211bafaafda": (5, "74611c1d6455b534", "kitap adı"),
    "ff832786b939ac14": (4, "86ae35d58a6aa3b5", "kitap adı (bölüm)"),
    "d0c8acf3d3fd0813": (2, "ff5c5e5c5caa5da2", "yazar"),
    "8cbe68ed50767702": (1, "8cbe68ed50767702", "yazar"),
    "9feea5dd114fcc9e": (1, "9feea5dd114fcc9e", "yazar (ders notu)"),
    "e49b43f6c96edef7": (1, "e49b43f6c96edef7", "yazar (ders notu)"),
    "0703a4fe531a46aa": (2, "f5195e75296086d2", "yazar"),
    "d17d3d443806a047": (2, "1bdfcd878e3778a5", "yazar"),
    "ff9be0bd5d77c45b": (3, "5f16795c54ab7de4", "yazar"),
    "1e8a7650c86649e2": (3, "18ac3e7343f01689", "yazar"),
    "7f559142904b66c0": (2, "381d9ef64788ee4d", "kişi adı (sözlükten çıkarıldı)"),
    "bb2ad99c5e6ad83e": (1, "bb2ad99c5e6ad83e", "tek kitaba özgü terim"),
    "563caa5289bf7be2": (2, "584a6898b0a5a613", "tek kitaba özgü terim"),
    "4e4283d3882c5a38": (1, "4e4283d3882c5a38", "tek kitaba özgü ad"),
    "4fee6bd88cad1037": (1, "4fee6bd88cad1037", "tek kitaba özgü ad"),
    "b4f802eee3ebdd99": (1, "b4f802eee3ebdd99", "tek kitaba özgü terim"),
    "bef9207962095cf3": (2, "b1f51a511f1da0cd", "tek kitaba özgü terim"),
    "508b0a36c8fb73d8": (2, "16477688c0e00699", "tek kitaba özgü terim"),
    "30c0b1ebb06560d9": (1, "30c0b1ebb06560d9", "makine adı"),
}
_ILK_SOZCUK: dict[str, set[int]] = {}
for _n, _ilk, _ in _YASAK_IFADELER.values():
    _ILK_SOZCUK.setdefault(_ilk, set()).add(_n)

# Başka kulvarların dosyaları (5 Ekim 2026 itibarıyla bu ifadeleri hâlâ
# taşıyor; dosya:satır listesi iç denetim notunda). Sahibi temizledikçe
# listeden çıkarılır; listede kalan temiz dosya testi bozmaz. 6 Ekim 2026:
# retranslator.py, autofix.py ve dört sözlük testi temizlendi, listeden çıktı.
_BASKA_KULVAR_DOSYALARI = {
    "latex_pipeline/translator.py",
    "tests/test_yol_audit_20260711.py",
    "tests/test_G2c_g.py",
    "tests/test_dalga_e1_20260925.py",
    "tests/test_fixav20260728_runtime.py",
    "tests/test_risk_layout_formats.py",
    "tests/test_risk_long_paragraphs.py",
    "tests/test_titlepage_fallback.py",
    "tests/test_yol_a_artifact_fixes.py",
    "tests/integration/test_hymt_real.py",
}


@lru_cache(maxsize=None)
def _ozet(ifade: str) -> str:
    return hashlib.sha256(ifade.encode("utf-8")).hexdigest()[:16]


def _norm(metin: str) -> str:
    return " ".join(s.lower() for s in _SOZCUK.findall(metin))


def _yasak_ifadeler(metin: str) -> list[str]:
    ss = [s.lower() for s in _SOZCUK.findall(metin)]
    bulunan = []
    for i, s in enumerate(ss):
        for n in _ILK_SOZCUK.get(_ozet(s), ()):
            tam = _ozet(" ".join(ss[i:i + n]))
            if tam in _YASAK_IFADELER:
                bulunan.append(_YASAK_IFADELER[tam][2])
    return bulunan


def test_ozet_tablosu_tutarli():
    for tam, (n, ilk, _) in _YASAK_IFADELER.items():
        assert len(tam) == 16 and len(ilk) == 16 and n >= 1
        if n == 1:
            assert tam == ilk
    # mekanizma: tabloda olmayan sıradan bir ifade bulunmaz
    assert _yasak_ifadeler("The nozzle throat area sets the mass flow rate.") == []


def test_acik_dosyalarda_kitap_ve_kisi_adi_yok():
    bulunan = []
    for p in _metin_dosyalari():
        rel = _goreli(p)
        if rel in _BASKA_KULVAR_DOSYALARI:
            continue
        es = _yasak_ifadeler(_oku(p))
        if es:
            bulunan.append(f"{rel}: {sorted(set(es))}")
    assert bulunan == [], bulunan


def test_kilavuz_pdf_kitap_ve_kisi_adi_yok():
    fitz = pytest.importorskip("fitz")
    with fitz.open(PROJE / "docs" / "Kullanma_Kilavuzu.pdf") as belge:
        metin = "\n".join(sayfa.get_text() for sayfa in belge)
    assert metin.strip()
    assert _yasak_ifadeler(metin) == []


# Sözlüklerden çıkarılan tek kitaba özgü girişler (K9 D3, KARAR 35): özetler.
_SOZLUKTEN_CIKARILAN = {
    "687ce1f9779525ed", "b4f802eee3ebdd99", "06e6aabf53ad0ec6", "d7a208ebefe87e53",
    "bef9207962095cf3", "2498c4827c248503", "508b0a36c8fb73d8", "61e8062f2dc12af8",
    "73599dfcdee72e5b", "782df2d85607c9c9", "5f128fc7130b1c6f", "c7df3e7fe1681544",
    "392f5dfc9a9c723e", "563caa5289bf7be2", "80439cc97f946563", "bb2ad99c5e6ad83e",
    "ee34b7f6d2a801b7", "835f023c77a4948b", "156958657771fb7f", "8b6f885158e53a7d",
    "f4db018dad0c24a6", "4e4283d3882c5a38", "4fee6bd88cad1037", "7f559142904b66c0",
    "f72e684372859ac1", "f6511c66063b4ca2",
}


def _sozluk_anahtarlari() -> list[str]:
    g = PROJE / "glossary"
    cur = json.loads((g / "curated.json").read_text(encoding="utf-8"))
    dnt = json.loads((g / "do_not_translate.json").read_text(encoding="utf-8"))
    return (list(cur["terms"]) + list(cur.get("do_not_translate", []))
            + list(dnt["terms"]))


def test_sozluklerde_tek_kitaba_ozgu_giris_yok():
    bulunan = [k for k in _sozluk_anahtarlari() if _ozet(_norm(k)) in _SOZLUKTEN_CIKARILAN]
    assert bulunan == [], bulunan
    cur = json.loads((PROJE / "glossary" / "curated.json").read_text(encoding="utf-8"))
    alanlar = {v.get("domain") for v in cur["terms"].values()}
    assert "philosophy" not in alanlar


def test_sozluklerde_genel_terimler_kalir():
    cur = json.loads((PROJE / "glossary" / "curated.json").read_text(encoding="utf-8"))
    for terim in ("nozzle", "transformer", "inner product", "machine learning",
                  "cognitive dissonance", "praxeology"):
        assert terim in cur["terms"], terim
    dnt = json.loads((PROJE / "glossary" / "do_not_translate.json").read_text(encoding="utf-8"))
    for ad in ("MIT", "serotonin"):
        assert ad in dnt["terms"], ad


# ---------------------------------------------------------------------------
# (3) İç belgeler ve açık belgeler
# ---------------------------------------------------------------------------

# Açık yayına uygun olmayan iç belgeler ve araç artıkları (dosyalar depoda kalır).
_IC_DESENLER = (
    "/docs/C5_LAYOUT_DETECTION_PLAN.md",
    "/docs/LATEX_PIPELINE_ARCHITECTURE.md",
    "/docs/PDF_TO_LATEX_TOOLS_COMPARISON.md",
    "/packaging/TASARIM.md",
    "/kutuphane/",
    "/.claude/",
    "/core/CLAUDE.md",
    "/parsers/CLAUDE.md",
    "/glossary/CLAUDE.md",
)
# Açık yayında kalması gereken kullanıcı belgeleri.
_KULLANICI_BELGELERI = (
    "README.md", "CEVIRI_TALIMATI.md", "CLAUDE.md", "LICENSE", ".gitattributes",
    "docs/KURULUM_VE_KULLANIM.md", "docs/Kullanma_Kilavuzu.tex",
    "docs/Kullanma_Kilavuzu.pdf", "docs/YENI_MODEL_EKLEME.md",
    "docs/DONANIM_VARSAYIMLARI.md",
)


@pytest.mark.parametrize("desen", _IC_DESENLER)
def test_ic_belge_export_ignore(desen):
    assert desen in _export_ignore_desenleri(), desen


@pytest.mark.parametrize("rel", _KULLANICI_BELGELERI)
def test_kullanici_belgesi_acik_yayinda(rel):
    assert (PROJE / rel).is_file(), rel
    assert not _export_ignore_mu(rel), rel


def _acik_belgeler() -> list[str]:
    belgeler = ["README.md", "CEVIRI_TALIMATI.md", "docs/Kullanma_Kilavuzu.tex"]
    belgeler += sorted(_goreli(p) for p in (PROJE / "docs").glob("*.md")
                       if not _export_ignore_mu(_goreli(p)))
    return belgeler


_MD_BAGLANTI = re.compile(r"\]\(\s*<?([^)\s>]+)>?(?:\s+\"[^\"]*\")?\s*\)")
_HTML_BAGLANTI = re.compile(r"(?:href|src)=\"([^\"]+)\"")
_TEX_BAGLANTI = re.compile(r"\\(?:href|url)\{([^}]+)\}")
# GitHub'ın arayüz yolları (depo dosyası değil): kök README'den göreli.
_GITHUB_ARAYUZ_YOLLARI = {"README.md": {"../../releases/latest"}}


def test_acik_belgelerin_goreli_baglantilari_acik_dosyaya_gider():
    bulunan = []
    for rel in _acik_belgeler():
        yol = PROJE / rel
        metin = _oku(yol)
        hedefler = (_MD_BAGLANTI.findall(metin) + _HTML_BAGLANTI.findall(metin)
                    + _TEX_BAGLANTI.findall(metin))
        for h in hedefler:
            if re.match(r"^(?:[a-z][a-z0-9+.-]*:|#)", h, re.I):
                continue                       # https:, mailto:, sayfa içi çapa
            if h in _GITHUB_ARAYUZ_YOLLARI.get(rel, set()):
                continue
            hedef = (yol.parent / h.split("#", 1)[0]).resolve()
            try:
                hedef_rel = hedef.relative_to(PROJE).as_posix()
            except ValueError:
                bulunan.append(f"{rel}: {h} (depo dışı)")
                continue
            if not hedef.exists():
                bulunan.append(f"{rel}: {h} (yok)")
            elif _export_ignore_mu(hedef_rel + ("/" if hedef.is_dir() else "")):
                bulunan.append(f"{rel}: {h} (açık yayında yok)")
    assert bulunan == [], bulunan


# İç süreç izleri: karar/bulgu numaraları, oturum klasörleri, ajan adları,
# açık yayında olmayan iç kurallar belgesi.
_IC_GONDERME = re.compile(
    r"KARARLAR|\bKARAR\s+\d|_oturum_|RAPOR\.md|KURALLAR\.md|\bkulvar"
    r"|Architect Agent|Codex Consultant|Gemini Consultant|\bMT-\d+\b",
    re.IGNORECASE)


def test_acik_belgelerde_ic_gonderme_yok():
    bulunan = []
    for rel in _acik_belgeler():
        for no, satir in enumerate(_oku(PROJE / rel).splitlines(), 1):
            m = _IC_GONDERME.search(satir)
            if m:
                bulunan.append(f"{rel}:{no}: {m.group(0)}")
    assert bulunan == [], bulunan


# ---------------------------------------------------------------------------
# (4) Depo adresi ve lisans özetleri
# ---------------------------------------------------------------------------

_DEPO_BELGELERI = ("README.md", "docs/KURULUM_VE_KULLANIM.md", "docs/Kullanma_Kilavuzu.tex")


@pytest.mark.parametrize("rel", _DEPO_BELGELERI)
def test_belgelerde_acik_depo_adresi(rel):
    metin = _oku(PROJE / rel).replace("\\_", "_")      # TeX: kitap\_cevir
    assert f"git clone {ACIK_DEPO}.git" in metin, rel
    assert "<depo adresi>" not in metin, rel


@pytest.mark.parametrize("rel", ("docs/KURULUM_VE_KULLANIM.md", "docs/Kullanma_Kilavuzu.tex"))
def test_alt_klasor_belgesinde_surum_sayfasi_mutlak(rel):
    """docs/ altındaki belgede (ve PDF'de) göreli ``../../releases`` yolu
    GitHub'da yanlış yere gider; sürüm sayfası mutlak adresle verilir."""
    metin = _oku(PROJE / rel).replace("\\_", "_")
    assert f"{ACIK_DEPO}/releases/latest" in metin, rel
    assert "../releases" not in metin, rel


# Kendi yazdığımız lisans özetlerinin izleri (KARAR 34: kaldırıldı; resmî
# metinlere bağlantı verilir).
_LISANS_OZETI_IZLERI = ("özettir", "Bu özet", "2 milyon", "100 milyon",
                        "hukukî danışmanlık", "hukuki danışmanlık")


@pytest.mark.parametrize("rel", _DEPO_BELGELERI)
def test_belgelerde_lisans_ozeti_yok(rel):
    metin = " ".join(_oku(PROJE / rel).split())
    bulunan = [iz for iz in _LISANS_OZETI_IZLERI if iz in metin]
    assert bulunan == [], (rel, bulunan)


def test_kilavuz_pdf_guncel():
    """PDF kılavuz .tex ile aynı içerikte (depo adresi, özet yok)."""
    fitz = pytest.importorskip("fitz")
    with fitz.open(PROJE / "docs" / "Kullanma_Kilavuzu.pdf") as belge:
        metin = " ".join("".join(sayfa.get_text() for sayfa in belge).split())
    duz = metin.replace("- ", "").replace(" ", "")
    assert "github.com/berketez/kitap_cevir" in duz
    assert "<depoadresi>" not in duz
    for iz in _LISANS_OZETI_IZLERI:
        assert iz.replace(" ", "") not in duz, iz
