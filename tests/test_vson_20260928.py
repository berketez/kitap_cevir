"""Son doğrulayıcı (Vson, 28 Eylül 2026) düzeltmelerinin regresyon testleri.

1. Yeniden çeviri kabul kapısı kaynak dile göre (K5 riski 1):
   ``AutoFixer._accept_retranslation`` İngilizce dışı kaynakta kaynak dilde
   kalmış adayı reddeder; İngilizcede karar ``is_english_line`` ile AYNI.
2. Yer tutucu geri yazımı TeX bağından (``~``) sonra/önce boşluk eklemez
   (Vk bulgusu 2: ``Section~ \\ref{x}``).
3. Toplu kip: bir kitapta model kullanılamaz olursa (çıkış 5) kalan kitaplar
   atlanır ve toplu kipin çıkış kodu 5 olur (Vk bulgusu 3).
4. Kullanıcıya dönük komut satırı metinlerinde "Yol A/B" iç adı yok; yol
   adları ``ui.branding``'den, dil listesi ``TEX_SOURCE_LANGS``'ten.
5. Türkçe EPUB hizalaması ``\\section[{kısa}]{uzun\\footnote{…}}`` başlığını
   başlık birimi sayar (Vq / C2-2).
6. Yabancı segment günlük iletisi dil-nötr ("kaynak dilinde değil").

Girdiler sentetiktir (kitap metni değil); model yüklenmez.
"""

from __future__ import annotations

import ast
import logging
import sys
from pathlib import Path
from unittest import mock

import pytest

import translate_book as tb
from core.lang_detect import TEX_SOURCE_LANGS, language_list_tr
from latex_pipeline.autofix import AutoFixer
from latex_pipeline.retranslator import is_english_line
from latex_pipeline.translator import LatexTranslator, TranslationStats
from ui import branding

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Sentetik satırlar (bu test için yazıldı)
DE_LINE = ("Die Geschwindigkeit des Körpers ist auf dieser Bahn immer gleich "
           "und hängt nicht von der Zeit ab.")
TR_LINE = ("Cismin bu yörüngedeki hızı her zaman aynıdır ve zamana bağlı "
           "olarak değişmez.")
EN_LINE = ("The speed of the body on this orbit is always the same and does "
           "not depend on time.")


# ---------------------------------------------------------------------------
# 1. Kabul kapısı
# ---------------------------------------------------------------------------
class TestKabulKapisiKaynakDil:
    def test_almanca_kaynakta_almanca_aday_reddedilir(self):
        # Eskiden yalnız İngilizce dedektörü soruluyordu -> KABUL ediliyordu.
        assert AutoFixer._accept_retranslation(
            "Eski satır burada.", DE_LINE, source_lang="de") is False

    def test_almanca_kaynakta_turkce_aday_kabul(self):
        assert AutoFixer._accept_retranslation(
            DE_LINE, TR_LINE, source_lang="de") is True

    def test_almanca_kaynakta_ingilizce_kalan_aday_da_reddedilir(self):
        # is_untranslated_line İngilizce kalmış satırı da çevrilmemiş sayar.
        assert AutoFixer._accept_retranslation(
            DE_LINE, EN_LINE, source_lang="de") is False

    @pytest.mark.parametrize("cand,heading", [
        (EN_LINE, False), (TR_LINE, False), (DE_LINE, False),
        ("The Shape of the Orbit", True), ("Yörüngenin Biçimi", True),
    ])
    def test_ingilizcede_karar_eskisiyle_ayni(self, cand, heading):
        expected = not is_english_line(cand, is_heading=heading)
        assert AutoFixer._accept_retranslation(
            "özgün satır", cand, is_heading=heading) is expected
        assert AutoFixer._accept_retranslation(
            "özgün satır", cand, is_heading=heading,
            source_lang="en") is expected

    def test_kaynak_dil_cevirmenden(self):
        fixer = AutoFixer.__new__(AutoFixer)
        fixer.translator = type("T", (), {"source_lang": "ru"})()
        assert fixer._source_lang() == "ru"
        fixer.translator = None
        assert fixer._source_lang() == "en"
        fixer.translator = mock.MagicMock()      # öznitelik metin değil
        assert fixer._source_lang() == "en"


# ---------------------------------------------------------------------------
# 2. TeX bağı
# ---------------------------------------------------------------------------
def _bare_lt() -> LatexTranslator:
    t = LatexTranslator.__new__(LatexTranslator)
    t._glossary_term_map = {}
    t.stats = TranslationStats()
    t.target_lang = "tr"
    return t


class TestBagBosluk:
    def test_bagdan_sonra_bosluk_yok(self):
        out = _bare_lt()._restore_stash("Bölüm~<<LATEX_0>> incelenir.",
                                        ["\\ref{sec:a}"])
        assert out == "Bölüm~\\ref{sec:a} incelenir."

    def test_bagdan_once_bosluk_yok(self):
        out = _bare_lt()._restore_stash("<<LATEX_0>>~numaralı şekil",
                                        ["\\ref{fig:b}"])
        assert out == "\\ref{fig:b}~numaralı şekil"

    def test_harf_bitisiginde_bosluk_garantisi_suruyor(self):
        out = _bare_lt()._restore_stash("önce<<LATEX_0>>sonra", ["\\cite{k}"])
        assert "önce \\cite{k} sonra" in out


# ---------------------------------------------------------------------------
# 3. Toplu kip ve çıkış 5
# ---------------------------------------------------------------------------
def _epub(path: Path) -> Path:
    import zipfile

    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("mimetype", "application/epub+zip")
        zf.writestr("OEBPS/ch0.xhtml",
                    "<html><body><h1>1</h1><p>Kurze Probe.</p></body></html>")
    return path


@pytest.fixture
def batch_env(tmp_path, monkeypatch):
    """Çıkarım/pipeline sahte; ikinci kitapta model kopar (çıkış 5)."""
    calls = {"extract": [], "pipeline": []}
    codes = iter([0, tb.EXIT_MODEL_UNAVAILABLE, 0, 0])

    def _extract_epub(epub_path, output_dir):
        calls["extract"].append(Path(epub_path).name)
        return True

    def _pipeline(output_dir, book_name, **kwargs):
        calls["pipeline"].append(book_name)
        return next(codes)

    monkeypatch.setattr(tb, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(tb, "run_epub_extraction", _extract_epub)
    monkeypatch.setattr(tb, "run_pipeline", _pipeline)
    monkeypatch.setattr(tb, "_source_language_pregate", lambda *a, **k: None)
    monkeypatch.delenv("KITAPCEVIR_EVENT_LOG", raising=False)
    books = [_epub(tmp_path / f"kitap{i}.epub") for i in range(1, 5)]
    return calls, books


class TestTopluKipModelKopmasi:
    def test_kalan_kitaplar_atlanir(self, batch_env, monkeypatch):
        calls, books = batch_env
        monkeypatch.setattr(sys, "argv", ["translate_book.py", str(books[0]),
                                          "--batch", *map(str, books)])
        results = tb.run_batch([str(b) for b in books], tb.parse_args())
        assert calls["extract"] == ["kitap1.epub", "kitap2.epub"]
        assert [Path(p).name for p, _t in results["success"]] == ["kitap1.epub"]
        assert [(Path(p).name, e) for p, e, _t in results["failed"]] == [
            ("kitap2.epub", f"exit={tb.EXIT_MODEL_UNAVAILABLE}")]
        assert [Path(p).name for p in results["skipped"]] == [
            "kitap3.epub", "kitap4.epub"]
        assert tb._batch_exit_code(results) == tb.EXIT_MODEL_UNAVAILABLE

    def test_main_cikis_5(self, batch_env, monkeypatch):
        _calls, books = batch_env
        monkeypatch.setattr(tb, "_engine_precheck_applies", lambda args: False)
        monkeypatch.setattr(sys, "argv", ["translate_book.py", str(books[0]),
                                          "--batch", *map(str, books)])
        with pytest.raises(SystemExit) as exc:
            tb.main()
        assert exc.value.code == tb.EXIT_MODEL_UNAVAILABLE

    def test_cikis_kodu_tablosu(self):
        assert tb._batch_exit_code({"failed": []}) == 0
        assert tb._batch_exit_code(
            {"failed": [("a.pdf", "exit=3", 1.0)]}) == 1
        assert tb._batch_exit_code(
            {"failed": [("a.pdf", "Extraction failed", 1.0)]}) == 1
        assert tb._batch_exit_code(
            {"failed": [("a.pdf", "exit=3", 1.0),
                        ("b.pdf", f"exit={tb.EXIT_MODEL_UNAVAILABLE}", 1.0)]}
        ) == tb.EXIT_MODEL_UNAVAILABLE


# ---------------------------------------------------------------------------
# 4. Kullanıcıya dönük komut satırı metinleri
# ---------------------------------------------------------------------------
_INTERNAL = ("Yol A", "Yol B", "YOL A", "YOL B", "ATLAS", "VECTOR")
# DONDURULMUŞ dosya (bu dalgada dokunulmadı): 2_derle.py başlığı ve --help
# açıklaması hâlâ "Yol A" diyor — ana oturumun işi (Vson raporu).
_FROZEN_ALLOW = {"2_derle.py"}


def _string_literals(path: Path):
    """Belge dizesi OLMAYAN metin sabitleri (kullanıcıya gidebilecek)."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    doc_ids = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.ClassDef,
                             ast.AsyncFunctionDef)) and node.body:
            first = node.body[0]
            if (isinstance(first, ast.Expr)
                    and isinstance(first.value, ast.Constant)
                    and isinstance(first.value.value, str)):
                doc_ids.add(id(first.value))
    for node in ast.walk(tree):
        if (isinstance(node, ast.Constant) and isinstance(node.value, str)
                and id(node) not in doc_ids):
            yield node.lineno, node.value


class TestKomutSatiriMetinleri:
    def test_yardim_metninde_ic_ad_yok(self, monkeypatch, capsys):
        monkeypatch.setattr(sys, "argv", ["translate_book.py", "--help"])
        with pytest.raises(SystemExit):
            tb.parse_args()
        text = capsys.readouterr().out
        for name in _INTERNAL:
            assert name not in text, name
        flat = " ".join(text.split())
        assert branding.TEX_TITLE in flat and branding.MODEL_TITLE in flat
        assert f"KAYNAK DİL: {language_list_tr(TEX_SOURCE_LANGS)}" in flat
        assert f"({', '.join(TEX_SOURCE_LANGS)});" in flat

    def test_modul_belgesi_yer_tutuculu(self):
        # Epilog biçimlenir; ham belge yer tutucuları taşır (tek kaynak).
        assert "{tex}" in tb.__doc__ and "{langs}" in tb.__doc__
        assert "{tex}" not in tb._cli_epilog()

    # run_full_pipeline.py 5 Ekim 2026'da ölü kod olarak silindi (KARAR 27).
    @pytest.mark.parametrize("rel", ["translate_book.py",
                                     "1_cikar.py", "app.py", "ui/runner.py",
                                     "ui/branding.py", "yol_a_ortak.py"])
    def test_metin_sabitlerinde_ic_ad_yok(self, rel):
        assert rel not in _FROZEN_ALLOW
        hits = [(ln, s[:60]) for ln, s in _string_literals(PROJECT_ROOT / rel)
                if any(name in s for name in _INTERNAL)]
        assert hits == [], hits

    def test_isaret_iletisi_yol_adini_brandingden_alir(self):
        # MANUAL_TRANSLATION.marker'lı kitapta --import-tex dışı koşu:
        # günlük iletisi TeX yolunun adını söyler, iç adı değil.
        src = (PROJECT_ROOT / "translate_book.py").read_text(encoding="utf-8")
        assert "bu kitap '%s' ile" in src
        assert '_branding_title("TEX_TITLE")' in src
        assert tb._branding_title("TEX_TITLE") == branding.TEX_TITLE


# ---------------------------------------------------------------------------
# 5. EPUB hizalaması: kısa başlık argümanlı başlık
# ---------------------------------------------------------------------------
class TestEpubBaslikBirimi:
    def test_kisa_argumanli_dipnotlu_baslik(self):
        from latex_pipeline import epub_output as eo

        tex = ("\\section[{Kısa başlık}]{Uzun başlık metni"
               "\\footnote{Başlığa düşülen not.}}\n")
        unit = eo._tex_units(tex)[0]
        assert unit.kind == "h:section"
        assert [(k, t) for _g, k, t in unit.notes] == [
            ("kaynak", "Başlığa düşülen not.")]

    @pytest.mark.parametrize("tex,kind", [
        ("\\section{Düz başlık}\n", "h:section"),
        ("\\chapter*[Kısa]{Uzun}\n", "h:chapter"),
        ("\\subsection*{Yıldızlı}\n", "h:subsection"),
    ])
    def test_eski_bicimler_ayni(self, tex, kind):
        from latex_pipeline import epub_output as eo

        assert eo._tex_units(tex)[0].kind == kind


# ---------------------------------------------------------------------------
# 6. Yabancı segment günlük iletisi
# ---------------------------------------------------------------------------
class TestYabanciSegmentIletisi:
    def test_ileti_dil_notr(self, caplog):
        t = _bare_lt()
        t.source_lang = "de"
        # Almanca kaynakta Kiril blok yabancı alıntıdır.
        ru = ("Это предложение написано по-русски и остаётся без перевода "
              "в немецкой книге.")
        with caplog.at_level(logging.INFO, logger="latex_pipeline.translator"):
            assert t._skip_foreign_segment(ru) is True
        text = caplog.text
        assert "kitabın kaynak dilinde değil" in text
        assert "İngilizce değil" not in text


# ---------------------------------------------------------------------------
# 7. Ölçülemeyen kısa belge: --kaynak-dil ipucu
# ---------------------------------------------------------------------------
class TestBelirsizDilIpucu:
    def test_und_uyarisinda_kaynak_dil_ipucu(self, caplog):
        # Vson uçtan uca koşusu: ~800 harflik Almanca belge "und" ölçüldü,
        # İngilizce varsayıldı, Almanca paragraflar alıntı sayılıp çevrilmedi
        # (QC çıkış 3). Uyarı artık çıkış yolunu söyler.
        log = logging.getLogger("vson.test.und")
        text = "Ein kurzer Satz. " * 5
        with caplog.at_level(logging.WARNING, logger="vson.test.und"):
            code, lang = tb._source_language_decision(text, log)
        assert (code, lang) == (None, "en")
        assert "--kaynak-dil" in caplog.text
        assert "belirlenemedi" in caplog.text


# ---------------------------------------------------------------------------
# 8. Kullanıcı dokümanında iç ad yok
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("rel", ["README.md", "docs/KURULUM_VE_KULLANIM.md",
                                 "docs/Kullanma_Kilavuzu.tex",
                                 "docs/YENI_MODEL_EKLEME.md"])
def test_kullanici_dokumaninda_ic_ad_yok(rel):
    # "Yol A/B" eşleme notları translate_book çıktısı temizlenince kaldırıldı
    # (Vson, 28 Eylül 2026; CLAUDE.md kuralı). 2_derle.py başlığı dondurulmuş
    # dosyada — ana oturumun işi.
    import re

    text = (PROJECT_ROOT / rel).read_text(encoding="utf-8")
    assert not re.search(r"\bYol [AB]\b|\bYOL [AB]\b|\bATLAS\b|\bVECTOR\b", text)
