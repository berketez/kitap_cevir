"""Dokümanlar, kart adı ve TDK ekleri (K5) — 25 Eylül 2026.

Kapsanan değişiklikler:
    K5a  README, docs/KURULUM_VE_KULLANIM.md ve Kullanma Kılavuzu yerel model
         yolunun iki dil kapısını (çıkarımdan önce ön kapı, sonra tam kapı),
         çıkış kodu 4'ü ve yalnız komut satırındaki ``--dil-kontrolu-yok``
         bayrağını anlatır; arayüzün çıkış 4 iletisiyle aynı sözü kullanır.
    K5b  "Sayfayı yenileyip kitabı yeniden seçin" tavsiyesi kalktı: TeX kartı
         dosyayı ``FILE_CHECK_SEC`` saniyede bir kendisi yokluyor.
    K5c  Kart adı "PDF'den TeX çıkar" (TDK); translate_book.py'nin dil kapısı
         iletileri adı ``ui.branding.TEX_TITLE``'dan okur.
    K5d  Kısaltmaya ek okunuşa göre gelir: PDF = pe-de-fe (ünlüyle biter).
         "PDF'i/PDF'in/PDF'e/PDF'te/PDF'ten/PDF'teki/PDF'tir" gibi eski
         biçimler kullanıcıya dönük kaynaklarda bulunmaz.

Testler MT modeli / XeLaTeX / output/ içeriği GEREKTİRMEZ.
"""

from __future__ import annotations

import ast
import logging
import re
import subprocess
import sys
from pathlib import Path

import pytest

import translate_book as tb
from core import lang_detect as ld
from core.lang_detect import LanguageEstimate
from ui import branding

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Ünsüzle biten okunuşa göre yazılmış eski ekler (PDF'i, PDF'in, PDF'e, PDF'te,
# PDF'ten, PDF'teki, PDF'tir, PDF'le, PDF'ini...). Çoğul (PDF'ler...) ve
# PDF'siz doğrudur, eşleşmez.
OLD_SUFFIX_RE = re.compile(
    r"PDF'(i|in|ini|inde|ine|inin|e|te|ten|teki|tir|le)"
    r"(?![A-Za-zçğıöşüÇĞİÖŞÜ])")

USER_FACING = sorted({
    PROJECT_ROOT / "ui" / "branding.py",
    PROJECT_ROOT / "ui" / "runner.py",
    PROJECT_ROOT / "app.py",
    PROJECT_ROOT / "translate_book.py",
    PROJECT_ROOT / "1_cikar.py",
    PROJECT_ROOT / "2_derle.py",
    PROJECT_ROOT / "README.md",
    PROJECT_ROOT / "CLAUDE.md",
    PROJECT_ROOT / "CEVIRI_TALIMATI.md",
    *(PROJECT_ROOT / "docs").rglob("*.md"),
    *(PROJECT_ROOT / "docs").glob("*.tex"),
})

# Yerel model yolunu anlatan kullanıcı dokümanları.
GATE_DOCS = ("README.md", "docs/KURULUM_VE_KULLANIM.md",
             "docs/Kullanma_Kilavuzu.tex")
TEX_DOCS = ("README.md", "docs/KURULUM_VE_KULLANIM.md",
            "docs/Kullanma_Kilavuzu.tex", "CEVIRI_TALIMATI.md", "CLAUDE.md")


def _read(relpath: str) -> str:
    return (PROJECT_ROOT / relpath).read_text(encoding="utf-8")


def _flat(text: str) -> str:
    """Satır sonlarını ve TeX'in ``-{}-`` yazımını düzleştir."""
    return " ".join(text.replace("-{}-", "--").split())


def _app_tree() -> ast.Module:
    return ast.parse(_read("app.py"))


def _function(tree: ast.Module, name: str) -> ast.FunctionDef:
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"app.py: {name} bulunamadı")


def _gate_error_prefix() -> str:
    """app._language_gate_failure'daki st.error iletisinin sabit baş kısmı."""
    func = _function(_app_tree(), "_language_gate_failure")
    for node in ast.walk(func):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "error"):
            arg = node.args[0]
            parts = arg.values if isinstance(arg, ast.JoinedStr) else [arg]
            text = "".join(p.value for p in parts
                           if isinstance(p, ast.Constant))
            return text.split(" (")[0]
    raise AssertionError("_language_gate_failure: st.error yok")


# ===========================================================================
# K5c — kart adı
# ===========================================================================

class TestK5cTexTitle:
    def test_title_is_tdk_form(self):
        assert branding.TEX_TITLE == "PDF'den TeX çıkar"

    @pytest.mark.parametrize("relpath", TEX_DOCS)
    def test_docs_use_current_title(self, relpath):
        assert branding.TEX_TITLE in _read(relpath), relpath

    def test_translate_book_does_not_hardcode_title(self):
        src = _read("translate_book.py")
        assert "TeX çıkar" not in src
        assert "TEX_TITLE" in src

    # K5 (2026-09-28, KARARLAR 14): yerel model TeX yoluyla aynı 6 kaynak
    # dili çevirir; dil kapısı iletileri artık TeX yolunu ÖNERMEZ (o yolun
    # listesi de aynı). Eski "adı branding'den okur" testlerinin yerini
    # "dil listesini tek kaynaktan (TEX_SOURCE_LANGS) okur" aldı.
    def test_stop_message_reads_language_list(self, monkeypatch, caplog):
        monkeypatch.setattr(ld, "TEX_SOURCE_LANGS", ("en", "de"))
        est = LanguageEstimate("pt", 0.99, letters=5000)
        log = logging.getLogger("k5.stop")
        with caplog.at_level(logging.INFO, logger="k5.stop"):
            tb._log_source_unsupported(est, log)
        errors = [r.getMessage() for r in caplog.records
                  if r.levelno == logging.ERROR]
        assert errors == [
            "Bu kitabın dili desteklenmiyor (tespit: Portekizce). Yerel model "
            "yalnız şu dillerden Türkçeye çevirir: İngilizce ve Almanca."]
        infos = [r.getMessage() for r in caplog.records
                 if r.levelno == logging.INFO]
        assert any("--kaynak-dil" in m and "--dil-kontrolu-yok" in m
                   for m in infos)

    def test_uncertain_warning_suggests_flag(self, monkeypatch, caplog):
        # Güven eşiğin altında: durdurmaz, İngilizce varsayar, --kaynak-dil'i
        # önerir (TeX yolu önerilmez).
        monkeypatch.setattr(
            ld, "analyze_language",
            lambda text: LanguageEstimate("de", 0.5, letters=5000))
        log = logging.getLogger("k5.warn")
        with caplog.at_level(logging.INFO, logger="k5.warn"):
            assert tb._source_language_gate("metin", log) is None
        warnings = [r.getMessage() for r in caplog.records
                    if r.levelno == logging.WARNING]
        assert len(warnings) == 1
        assert "--kaynak-dil" in warnings[0]
        assert branding.TEX_TITLE not in warnings[0]

    def test_real_message_in_fresh_interpreter(self):
        # Döngüsel içe aktarma olmadan, gerçek dil listesiyle (modüller
        # önceden yüklenmemiş temiz süreçte).
        code = (
            "import logging, sys\n"
            "logging.basicConfig(stream=sys.stdout, level=logging.INFO,"
            " format='%(message)s')\n"
            "import translate_book as tb\n"
            "from core.lang_detect import LanguageEstimate\n"
            "tb._log_source_unsupported(LanguageEstimate('cjk', 0.99,"
            " letters=5000), logging.getLogger('k5'))\n"
        )
        out = subprocess.run([sys.executable, "-c", code], cwd=PROJECT_ROOT,
                             capture_output=True, text=True, timeout=120)
        assert out.returncode == 0, out.stderr
        assert ("Bu kitabın dili desteklenmiyor (tespit: Çince/Japonca/Korece). "
                "Yerel model yalnız şu dillerden Türkçeye çevirir: İngilizce, "
                "Almanca, Rusça, Fransızca, İspanyolca ve İtalyanca."
                in out.stdout)

    def test_branding_does_not_import_pipeline(self):
        tree = ast.parse(_read("ui/branding.py"))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                imported.add(node.module or "")
            elif isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
        # pathlib: stdlib (K6, 2026-10-05: "Modelleri tara" açıklamasındaki
        # klasör yolunun biçimi); boru hattı içe aktarımı yine yok.
        assert imported <= {"__future__", "core.lang_detect", "pathlib"}, imported


# ===========================================================================
# K5d — TDK ekleri
# ===========================================================================

class TestK5dSuffixes:
    @pytest.mark.parametrize(
        "path", USER_FACING,
        ids=[str(p.relative_to(PROJECT_ROOT)) for p in USER_FACING])
    def test_no_old_suffix(self, path):
        hits = [f"{i}: {line.strip()}"
                for i, line in enumerate(
                    path.read_text(encoding="utf-8").splitlines(), 1)
                if OLD_SUFFIX_RE.search(line)]
        assert hits == [], hits

    @pytest.mark.parametrize("old,new", [
        ("PDF'i aç", "PDF'yi aç"),
        ("kaynak PDF'in sayfası", "kaynak PDF'nin sayfası"),
        ("Türkçe PDF'e", "Türkçe PDF'ye"),
        ("PDF'te sayfa sayısı", "PDF'de sayfa sayısı"),
        ("PDF'ten TeX çıkar", "PDF'den TeX çıkar"),
        ("PDF'teki", "PDF'deki"),
        ("bir PDF'tir.", "bir PDF'dir."),
        ("PDF'inde", "PDF'sinde"),
    ])
    def test_pattern_catches_old_not_new(self, old, new):
        assert OLD_SUFFIX_RE.search(old)
        assert not OLD_SUFFIX_RE.search(new)

    @pytest.mark.parametrize("text", [
        "PDF'ler", "PDF'leri", "PDF'lerde", "PDF'siz", "PDF'yle",
        "PDF'yi", "PDF'nin", "PDF'sinin"])
    def test_pattern_leaves_correct_forms(self, text):
        assert not OLD_SUFFIX_RE.search(text)

    def test_open_button_label_matches_docs(self):
        labels = [node.args[0].value for node in ast.walk(_app_tree())
                  if isinstance(node, ast.Call)
                  and isinstance(node.func, ast.Attribute)
                  and node.func.attr == "button"
                  and any(k.arg == "key" and getattr(k.value, "value", None)
                          == "pdf_open" for k in node.keywords)]
        assert labels == ["PDF'yi aç"]
        for relpath in ("README.md", "docs/KURULUM_VE_KULLANIM.md",
                        "docs/Kullanma_Kilavuzu.tex", "CLAUDE.md"):
            assert "PDF'yi aç" in _read(relpath), relpath


# ===========================================================================
# K5a — dil kapıları dokümanda
# ===========================================================================

class TestK5aGateDocs:
    def test_app_message_prefix(self):
        # K5 (2026-09-28): çıkış 4 = desteklenmeyen dil.
        assert _gate_error_prefix() == (
            "Yerel model bu kitabı çevirmedi: kitabın dili desteklenmiyor")

    @pytest.mark.parametrize("relpath", GATE_DOCS)
    def test_doc_describes_both_gates(self, relpath):
        text = _flat(_read(relpath))
        low = text.casefold()
        assert "ön kapı" in low and "tam kapı" in low, relpath
        assert "çıkarımdan önce" in low and "çıkarımdan sonra" in low
        assert f"çıkış kodu {tb.EXIT_SOURCE_NOT_ENGLISH}" in text
        assert "--dil-kontrolu-yok" in text
        assert "--kaynak-dil" in text         # K5: elle kaynak dil
        assert "arayüzde yok" in low          # bayrak yalnız komut satırında
        assert _gate_error_prefix() in text   # arayüzle aynı söz

    def test_flag_exists_on_cli(self, monkeypatch):
        monkeypatch.setattr(sys, "argv",
                            ["translate_book.py", "kitap.pdf",
                             "--dil-kontrolu-yok"])
        assert tb.parse_args().dil_kontrolu_yok is True

    def test_exit_code_table_matches_docstring(self):
        doc_codes = {int(m.group(1)) for m in re.finditer(
            r"(?m)^\s{4}(\d+)\s{2,}\S", tb.__doc__)}
        assert tb.EXIT_SOURCE_NOT_ENGLISH in doc_codes
        text = _read("docs/KURULUM_VE_KULLANIM.md")
        table = text.split("`translate_book.py` çıkış kodları:", 1)[1]
        table = table.split("\n\n", 2)[1]
        md_codes = {int(m.group(1)) for m in re.finditer(
            r"(?m)^\| (\d+) \|", table)}
        assert md_codes == doc_codes

    def test_options_table_lists_flag(self):
        text = _read("docs/KURULUM_VE_KULLANIM.md")
        section = text.split("### 9.2.", 1)[1].split("## 10.", 1)[0]
        assert re.search(r"(?m)^\| `--dil-kontrolu-yok` \|", section)


# ===========================================================================
# K5b — TeX kartı dosyayı kendisi yokluyor
# ===========================================================================

class TestK5bNoReloadAdvice:
    @pytest.mark.parametrize("relpath", [
        "README.md", "docs/KURULUM_VE_KULLANIM.md",
        "docs/Kullanma_Kilavuzu.tex"])
    def test_stale_advice_removed(self, relpath):
        text = _flat(_read(relpath))
        assert "yenileyip kitabı yeniden seçin" not in text

    @pytest.mark.parametrize("relpath", [
        "docs/KURULUM_VE_KULLANIM.md", "docs/Kullanma_Kilavuzu.tex"])
    def test_new_advice_present(self, relpath):
        text = _flat(_read(relpath))
        assert text.count("birkaç saniye içinde kendiliğinden açılır") == 2
        assert "TeX'i çıkarırken kullandığınızla aynı" in text

    def test_tex_card_polls_file(self):
        tree = _app_tree()
        consts = {t.id: node.value.value for node in tree.body
                  if isinstance(node, ast.Assign)
                  for t in node.targets if isinstance(t, ast.Name)
                  and isinstance(node.value, ast.Constant)}
        # "Birkaç saniye" sözü bu sınırla doğru kalır.
        assert 0 < consts["FILE_CHECK_SEC"] <= 10
        card = _function(tree, "_tex_card")
        decorators = [ast.unparse(d) for d in card.decorator_list]
        assert "st.fragment(run_every=FILE_CHECK_SEC)" in decorators
