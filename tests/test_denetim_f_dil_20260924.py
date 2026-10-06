"""Denetim 24 Eylül 2026 — Kulvar F: dil kapısı ve varsayılan motor.

Kaynak: 24 Eylül denetimi (iç denetim notu).

    F1 (Mercek 3 #14) should_stop_for_source_language eşik sınırı.
    F2 (Mercek 3 #15) Desteklenen kaynak dil listesi tek sabitte
       (core.lang_detect.TEX_SOURCE_LANGS); arayüz ve 1_cikar.py metni ondan
       üretilir, dokümanlardaki listeler bu sabitle eşleşir.
    F3 (Mercek 2 #7)  Çıkarım ÖNCESİ kaynak dil ön kapısı (PDF metin
       katmanı / EPUB metni): İngilizce olmayan kitap Marker OCR'a girmeden
       çıkış kodu 4 ile durur; Yol A (--export-tex / --import-tex /
       1_cikar.py) ve --dil-kontrolu-yok ön kapıyı hiç çalıştırmaz.
       28 Eylül 2026 (K5, KARARLAR 14): yerel model yolu TEX_SOURCE_LANGS'i
       çevirir; kapılar yalnız DESTEKLENMEYEN dilde durdurur (burada
       sentetik Portekizce). --kaynak-dil de ön kapıyı atlar.
    F4 (Mercek 3 #4)  translate_book'taki motor varsayılanları
       core.mt_engines.registry.DEFAULT_ENGINE'den gelir.

Örnek metinler bu test için yazılmış sentetik paragraflardır (aynı içerik
dört dilde); hiçbir kitaptan alınmamıştır. Testler MT modeli / XeLaTeX /
Marker GEREKTİRMEZ: çıkarım ve pipeline fonksiyonları sahte nesnelerle
değiştirilir, PDF'ler PyMuPDF ile test sırasında üretilir.
"""

from __future__ import annotations

import importlib.util
import inspect
import json
import logging
import math
import re
import sys
import zipfile
from pathlib import Path

import fitz
import pytest

import translate_book as tb
from core import lang_detect as ld
from core.mt_engines import registry
from ui import branding

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# ===========================================================================
# Sentetik paragraflar (bu test için yazıldı)
# ===========================================================================

_PARAS = {
    "en": (
        "The temperature was measured again every morning, because the "
        "values changed a lot during the night. We checked the instruments "
        "before the experiment and collected the results in a table. It "
        "turned out that the pressure decreases with altitude and that the "
        "air is clearly colder at the top. This observation is not new, but "
        "it is reproduced here for the first time with simple means. One can "
        "therefore say that a small laboratory can also reach reliable "
        "conclusions when the work is documented carefully. In the next "
        "section the limits of the method are discussed, since every method "
        "has its weaknesses."
    ),
    "de": (
        "Die Messung der Temperatur wurde an jedem Morgen wiederholt, weil "
        "die Werte in der Nacht stark schwankten. Wir haben die Geräte vor "
        "dem Versuch sorgfältig geprüft und die Ergebnisse in einer Tabelle "
        "gesammelt. Es zeigte sich, dass der Druck mit der Höhe abnimmt und "
        "dass die Luft oben deutlich kälter ist. Diese Beobachtung ist nicht "
        "neu, aber sie wird hier zum ersten Mal mit einfachen Mitteln "
        "nachvollzogen. Man kann daher sagen, dass auch ein kleines Labor zu "
        "verlässlichen Aussagen kommen kann, wenn die Arbeit genau "
        "dokumentiert wird. Im nächsten Abschnitt werden die Grenzen des "
        "Verfahrens besprochen, denn jede Methode hat ihre Schwächen."
    ),
    "fr": (
        "La mesure de la température a été répétée chaque matin, parce que "
        "les valeurs changeaient beaucoup pendant la nuit. Nous avons vérifié "
        "les appareils avant l'essai et nous avons rassemblé les résultats "
        "dans un tableau. Il est apparu que la pression diminue avec "
        "l'altitude et que l'air est nettement plus froid en haut. Cette "
        "observation n'est pas nouvelle, mais elle est ici reproduite pour la "
        "première fois avec des moyens simples. On peut donc dire qu'un petit "
        "laboratoire peut aussi obtenir des conclusions fiables, lorsque le "
        "travail est bien documenté. Dans la section suivante, nous discutons "
        "des limites de la méthode, car chaque méthode a ses faiblesses."
    ),
    # Portekizce: tanınan (profili var) ama DESTEKLENMEYEN kaynak dil (K5).
    "pt": (
        "A medição da temperatura foi repetida todas as manhãs, porque os "
        "valores mudavam muito durante a noite. Verificamos os aparelhos "
        "antes do ensaio e reunimos os resultados numa tabela. Verificou-se "
        "que a pressão diminui com a altitude e que o ar é claramente mais "
        "frio no alto. Esta observação não é nova, mas é aqui reproduzida "
        "pela primeira vez com meios simples. Pode-se portanto dizer que "
        "também um pequeno laboratório pode chegar a conclusões fiáveis, "
        "quando o trabalho é bem documentado. Na secção seguinte são "
        "discutidos os limites do método, porque cada método tem as suas "
        "fraquezas."
    ),
    "ru": (
        "Измерение температуры повторяли каждое утро, потому что значения "
        "сильно менялись ночью. Мы проверили приборы перед опытом и собрали "
        "результаты в таблицу. Оказалось, что давление уменьшается с высотой "
        "и что воздух наверху заметно холоднее. Это наблюдение не новое, но "
        "здесь оно впервые воспроизведено простыми средствами. Поэтому можно "
        "сказать, что и небольшая лаборатория может прийти к надёжным "
        "выводам, если работа тщательно документирована. В следующем разделе "
        "обсуждаются границы метода, так как у каждого метода есть свои "
        "слабости."
    ),
}

# Yanlış OCR diliyle üretilmiş metin katmanı benzetimi: İngilizce paragrafın
# her harfi Kiril benzeriyle değiştirilir ("Тне темрегатиге шаѕ ...").
# Alfabe payı %100 Kiril ama Rusça işlev kelimesi kanıtı yok denecek kadar az.
_HOMOGLYPHS = dict(zip("abcdefghijklmnopqrstuvwxyz",
                       "аьсдефднијклмпорфгѕтившхуз"))
_HOMOGLYPHS.update({k.upper(): v.upper() for k, v in _HOMOGLYPHS.items()})
_GARBLED_CYRILLIC = "".join(_HOMOGLYPHS.get(c, c) for c in _PARAS["en"])


def _repeat(lang: str, times: int = 12) -> str:
    return "\n\n".join([_PARAS[lang]] * times)


def _make_pdf(path: Path, lang: str | None, pages: int = 6,
              per_page: int = 3) -> Path:
    """Latin harfli metin katmanlı PDF (lang=None: metinsiz, yalnız çizim)."""
    doc = fitz.open()
    for _ in range(pages):
        page = doc.new_page()
        if lang is None:
            page.draw_rect(fitz.Rect(100, 100, 300, 300), color=(0, 0, 0))
            continue
        text = "\n\n".join([_PARAS[lang]] * per_page)
        rc = page.insert_textbox(fitz.Rect(50, 50, 550, 800), text,
                                 fontname="helv", fontsize=9)
        assert rc >= 0, "metin sayfaya sığmadı"
    doc.save(str(path))
    doc.close()
    return path


def _make_epub(path: Path, lang: str, chapters: int = 4) -> Path:
    """Asgari EPUB: (X)HTML bölümlü zip (ön kapı yalnız metne bakar)."""
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("mimetype", "application/epub+zip")
        for i in range(chapters):
            body = "".join(f"<p>{_PARAS[lang]}</p>" for _ in range(3))
            zf.writestr(
                f"OEBPS/ch{i}.xhtml",
                "<?xml version='1.0' encoding='utf-8'?><html><head>"
                "<title>Head English title words</title><style>p{}</style>"
                f"</head><body><h1>{i}</h1>{body}</body></html>",
            )
    return path


# ===========================================================================
# F1 — tam kapı eşik sınırı
# ===========================================================================

class TestGateThresholdBoundary:
    # K5 (2026-09-28, KARARLAR 14): durdurma yalnız DESTEKLENMEYEN dilde
    # (eskiden her İngilizce-dışı dilde; testler "de" ile yazılmıştı).
    def test_exactly_threshold_stops(self):
        est = ld.LanguageEstimate("pt", ld.GATE_STOP_CONFIDENCE)
        assert ld.should_stop_for_source_language(est) is True

    def test_just_below_threshold_warns_only(self):
        below = math.nextafter(ld.GATE_STOP_CONFIDENCE, 0.0)
        est = ld.LanguageEstimate("pt", below)
        assert ld.should_stop_for_source_language(est) is False

    @pytest.mark.parametrize("code", ld.TEX_SOURCE_LANGS)
    def test_supported_language_never_stops(self, code):
        assert ld.should_stop_for_source_language(
            ld.LanguageEstimate(code, 1.0)) is False

    def test_undetermined_never_stops(self):
        assert ld.should_stop_for_source_language(
            ld.LanguageEstimate("und", 1.0)) is False

    def test_english_never_stops(self):
        assert ld.should_stop_for_source_language(
            ld.LanguageEstimate("en", 1.0)) is False

    def test_script_level_code_at_threshold_stops(self):
        est = ld.LanguageEstimate("cyrillic", ld.GATE_STOP_CONFIDENCE)
        assert ld.should_stop_for_source_language(est) is True


# ===========================================================================
# F2 — desteklenen kaynak dil listesi tek sabitte
# ===========================================================================

_NAME = r"[A-ZÇĞİÖŞÜ][a-zçğıöşü]*(?:ca|ce|ça|çe)"
_SEP = r"(?:\s*,\s*|\s+ve\s+|\s+ya\s+da\s+)"
_LIST_RE = re.compile(rf"{_NAME}(?:{_SEP}{_NAME})+")


def _language_lists(text: str) -> list[list[str]]:
    """Metindeki "İngilizce, Almanca, ... ya da X" biçimli dil listeleri.

    Markdown vurgusu (``**``) ve alıntı (``>``) işaretleri atılır; yalnız
    İngilizce ile başlayan en az üç adlı listeler döner (desteklenen dil
    listesinin imzası).
    """
    text = re.sub(r"(?m)^\s*>\s?", "", text).replace("**", "")
    out = []
    for m in _LIST_RE.finditer(text):
        names = re.split(_SEP, m.group(0))
        if names[0] == "İngilizce" and len(names) >= 3:
            out.append(names)
    return out


def _expected_names() -> list[str]:
    return [ld.language_name_tr(code) for code in ld.TEX_SOURCE_LANGS]


def _load_cikar():
    spec = importlib.util.spec_from_file_location(
        "cikar_denetim_f", PROJECT_ROOT / "1_cikar.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestSupportedLanguagesSingleSource:
    def test_constant_value(self):
        assert ld.TEX_SOURCE_LANGS == ("en", "de", "ru", "fr", "es", "it")
        # CJK kapsam dışı (tasarım kararı); Portekizce yalnız TESPİT edilir.
        assert "cjk" not in ld.TEX_SOURCE_LANGS
        assert "pt" in ld.EUROPEAN_DETECT_LANGS
        assert "pt" not in ld.TEX_SOURCE_LANGS
        for code in ld.TEX_SOURCE_LANGS:
            assert code in ld.LANGUAGE_NAMES_TR, code

    def test_old_misleading_name_removed(self):
        assert not hasattr(ld, "EUROPEAN_SOURCE_LANGS")
        for path in (PROJECT_ROOT / "core" / "lang_detect.py",
                     PROJECT_ROOT / "latex_pipeline" / "qc_engine.py",
                     PROJECT_ROOT / "translate_book.py"):
            assert "EUROPEAN_SOURCE_LANGS" not in path.read_text(
                encoding="utf-8"), path

    def test_language_list_formatting(self):
        assert ld.language_list_tr(("en", "de", "ru"), " ya da ") == (
            "İngilizce, Almanca ya da Rusça")
        assert ld.language_list_tr(("en", "de")) == "İngilizce, Almanca"
        assert ld.language_list_tr(("ru",)) == "Rusça"
        assert ld.language_list_tr(()) == ""

    @pytest.mark.parametrize("relpath", [
        "CEVIRI_TALIMATI.md",
        "README.md",
        "docs/KURULUM_VE_KULLANIM.md",
        "docs/Kullanma_Kilavuzu.tex",
    ])
    def test_docs_match_constant(self, relpath):
        text = (PROJECT_ROOT / relpath).read_text(encoding="utf-8")
        lists = _language_lists(text)
        assert lists, f"{relpath}: desteklenen dil listesi bulunamadı"
        for names in lists:
            assert names == _expected_names(), (relpath, names)

    def test_branding_generated_from_constant(self):
        assert _language_lists(branding.TEX_DESCRIPTION) == [_expected_names()]
        # Metin elle yazılmıyor: kaynak kodda dil adı geçmiyor.
        src = inspect.getsource(branding)
        for name in _expected_names()[1:]:
            assert name not in src, name

    def test_1_cikar_texts_match_constant(self):
        cikar = _load_cikar()
        description = cikar.build_parser().description
        assert _language_lists(description) == [_expected_names()]
        assert ld.language_list_tr(ld.TEX_SOURCE_LANGS, " ya da ") in (
            " ".join(description.split()))
        # Modül docstring'indeki düzyazı liste de sabitle aynı.
        assert _language_lists(cikar.__doc__) == [_expected_names()]


# ===========================================================================
# F3 — çıkarım öncesi ön kapı
# ===========================================================================

class TestPregateDecision:
    """core.lang_detect.should_stop_before_extraction (birim)."""

    def _est(self, code="pt", conf=1.0, letters=None, coverage=None):
        return ld.LanguageEstimate(
            code, conf,
            letters=ld.PREGATE_MIN_LETTERS if letters is None else letters,
            coverage=(ld.PROFILE_FULL_COVERAGE if coverage is None
                      else coverage))

    def test_all_conditions_at_boundary_stop(self):
        assert ld.should_stop_before_extraction(self._est()) is True

    def test_below_gate_confidence_passes(self):
        below = math.nextafter(ld.GATE_STOP_CONFIDENCE, 0.0)
        assert ld.should_stop_before_extraction(self._est(conf=below)) is False

    def test_too_few_letters_passes(self):
        est = self._est(letters=ld.PREGATE_MIN_LETTERS - 1)
        assert ld.should_stop_before_extraction(est) is False

    def test_low_word_coverage_passes(self):
        below = math.nextafter(ld.PROFILE_FULL_COVERAGE, 0.0)
        assert ld.should_stop_before_extraction(
            self._est(coverage=below)) is False

    def test_script_without_profile_never_prestops(self):
        """CJK/Yunan/Arap: kelime profili yok → ön kapı durdurmaz."""
        for code in ("cjk", "el", "arabic", "hebrew"):
            est = self._est(code=code, coverage=0.0)
            assert ld.should_stop_before_extraction(est) is False, code

    def test_pregate_stricter_than_full_gate(self):
        assert ld.PREGATE_MIN_LETTERS > ld.DETECT_MIN_LETTERS

    def test_real_measurement_stops(self):
        est = ld.analyze_language(_repeat("pt"), already_clean=True)
        assert est.code == "pt"
        assert ld.should_stop_before_extraction(est)

    @pytest.mark.parametrize("lang", ["de", "fr", "ru"])
    def test_supported_measurement_passes(self, lang):
        # K5: desteklenen dil ön kapıda durmaz; dil tam kapıda zincire akar.
        est = ld.analyze_language(_repeat(lang), already_clean=True)
        assert est.code == lang
        assert est.confidence >= ld.GATE_STOP_CONFIDENCE
        assert not ld.should_stop_before_extraction(est)

    def test_english_measurement_passes(self):
        est = ld.analyze_language(_repeat("en"), already_clean=True)
        assert est.code == "en"
        assert not ld.should_stop_before_extraction(est)

    def test_garbled_ocr_layer_does_not_prestop(self):
        """Yanlış OCR diliyle Kiril görünümlü çöp katman: tam kapı onu
        'Kiril alfabeli' diye durdururdu, ön kapı kelime kanıtı aradığı için
        geçer (karar Marker çıktısını ölçen tam kapıya kalır)."""
        est = ld.analyze_language(
            "\n\n".join([_GARBLED_CYRILLIC] * 12), already_clean=True)
        assert est.code == "cyrillic"
        assert ld.should_stop_for_source_language(est)
        assert est.coverage < ld.PROFILE_FULL_COVERAGE
        assert not ld.should_stop_before_extraction(est)

    def test_coverage_field_reported(self):
        est = ld.analyze_language(_repeat("ru"), already_clean=True)
        assert est.coverage >= ld.RUSSIAN_MIN_COVERAGE
        est = ld.analyze_language(_repeat("de"), already_clean=True)
        assert est.coverage == round(est.family_hits / est.words, 3)

    def test_plain_text_cleaning_keeps_percent_lines(self):
        """%50 gibi ifadeler düz metinde satırın geri kalanını silmemeli."""
        text = "Growth was 50% faster than the rest of the market."
        assert "faster" in ld.clean_plain_text_for_detection(text)
        assert "faster" not in ld.clean_latex_for_detection(text)
        assert "example.org" not in ld.clean_plain_text_for_detection(
            "see https://example.org/page now")


class _Recorder:
    def __init__(self):
        self.extraction = []
        self.epub_extraction = []
        self.pipeline = []


@pytest.fixture
def flow(monkeypatch):
    """Çıkarım ve pipeline'ı sahte nesnelerle değiştir; çağrıları kaydet."""
    rec = _Recorder()

    def _extract(pdf_path, output_dir, batch_size, device):
        rec.extraction.append(pdf_path)
        return True

    def _extract_epub(epub_path, output_dir):
        rec.epub_extraction.append(epub_path)
        return True

    def _pipeline(output_dir, book_name, **kwargs):
        rec.pipeline.append(kwargs)
        return 0

    monkeypatch.setattr(tb, "run_extraction", _extract)
    monkeypatch.setattr(tb, "run_epub_extraction", _extract_epub)
    monkeypatch.setattr(tb, "run_pipeline", _pipeline)
    monkeypatch.delenv("KITAPCEVIR_EVENT_LOG", raising=False)
    yield rec
    pregate_log = logging.getLogger("pipeline.pregate")
    assert not pregate_log.handlers, "ön kapı günlük handler'larını bıraktı"


def _main(monkeypatch, *argv) -> int:
    monkeypatch.setattr(sys, "argv", ["translate_book.py", *map(str, argv)])
    with pytest.raises(SystemExit) as exc:
        tb.main()
    return exc.value.code


# K5 (2026-09-28): durdurma iletisi "desteklenmeyen dil" der; dil listesi tek
# kaynaktan (TEX_SOURCE_LANGS).
_STOP_MSG = ("Bu kitabın dili desteklenmiyor (tespit: {}). Yerel model yalnız "
             "şu dillerden Türkçeye çevirir: "
             + ld.language_list_tr(ld.TEX_SOURCE_LANGS, " ve ") + ".")


class TestPregateFlow:
    def test_unsupported_pdf_stops_before_extraction(
            self, tmp_path, monkeypatch, capsys, flow):
        pdf = _make_pdf(tmp_path / "livro.pdf", "pt")
        out = tmp_path / "out"
        code = _main(monkeypatch, pdf, "--output-dir", out)
        assert code == tb.EXIT_SOURCE_UNSUPPORTED
        assert flow.extraction == [], "Marker çıkarımı çağrılmamalıydı"
        assert flow.pipeline == []
        msg = _STOP_MSG.format("Portekizce")
        assert msg in capsys.readouterr().out
        log = (out / "pipeline.log").read_text(encoding="utf-8")
        assert msg in log and "Kaynak dil ön kapısı" in log
        assert "--dil-kontrolu-yok" in log and "--kaynak-dil" in log
        # Reddedilen kitabın kopyası/çıkarımı yapılmaz.
        assert not (out / "source_book.pdf").exists()

    @pytest.mark.parametrize("lang", ["de", "fr"])
    def test_supported_pdf_passes_pregate(self, tmp_path, monkeypatch, capsys,
                                          flow, lang):
        # K5: eskiden Almanca/Fransızca PDF burada çıkış 4 alıyordu.
        pdf = _make_pdf(tmp_path / f"kitap_{lang}.pdf", lang)
        out = tmp_path / "out"
        assert _main(monkeypatch, pdf, "--output-dir", out) == 0
        assert flow.extraction == [str(pdf.resolve())]
        assert flow.pipeline and flow.pipeline[0]["source_lang"] is None
        assert "ön kapı" not in capsys.readouterr().out
        assert not (out / "pipeline.log").exists()

    def test_kaynak_dil_flag_skips_pregate_and_is_forwarded(
            self, tmp_path, monkeypatch, flow):
        pdf = _make_pdf(tmp_path / "livro.pdf", "pt")
        assert _main(monkeypatch, pdf, "--output-dir", tmp_path / "out",
                     "--kaynak-dil", "es") == 0
        assert flow.extraction
        assert flow.pipeline[0]["source_lang"] == "es"

    def test_kaynak_dil_rejects_unsupported_code(self, monkeypatch, capsys):
        monkeypatch.setattr(sys, "argv", ["translate_book.py", "kitap.pdf",
                                          "--kaynak-dil", "pt"])
        with pytest.raises(SystemExit) as exc:
            tb.parse_args()
        assert exc.value.code == 2
        assert "--kaynak-dil" in capsys.readouterr().err

    def test_error_reaches_event_stream(self, tmp_path, monkeypatch, flow):
        events = tmp_path / "events.jsonl"
        monkeypatch.setenv("KITAPCEVIR_EVENT_LOG", str(events))
        pdf = _make_pdf(tmp_path / "livro.pdf", "pt")
        assert _main(monkeypatch, pdf, "--output-dir",
                     tmp_path / "out") == tb.EXIT_SOURCE_UNSUPPORTED
        rows = [json.loads(line) for line in
                events.read_text(encoding="utf-8").splitlines() if line]
        errors = [r for r in rows if r.get("kind") == "error"]
        assert errors and errors[-1]["message"] == _STOP_MSG.format(
            "Portekizce")

    def test_english_pdf_passes_silently(self, tmp_path, monkeypatch,
                                         capsys, flow):
        pdf = _make_pdf(tmp_path / "book.pdf", "en")
        out = tmp_path / "out"
        assert _main(monkeypatch, pdf, "--output-dir", out) == 0
        assert flow.extraction == [str(pdf.resolve())]
        assert flow.pipeline and flow.pipeline[0]["source_lang_check"] is True
        assert "ön kapı" not in capsys.readouterr().out
        # Geçen koşuda ön kapı pipeline.log'a dokunmaz.
        assert not (out / "pipeline.log").exists()

    def test_textless_pdf_passes(self, tmp_path, monkeypatch, flow):
        pdf = _make_pdf(tmp_path / "tarama.pdf", None)
        assert _main(monkeypatch, pdf, "--output-dir", tmp_path / "out") == 0
        assert flow.extraction

    def test_broken_pdf_passes(self, tmp_path, monkeypatch, flow):
        """Açılamayan dosyada ön kapı sessizce geçer (tam kapı sonra ölçer)."""
        bad = tmp_path / "bozuk.pdf"
        bad.write_bytes(b"%PDF-1.4\n" + b"x" * 2048)
        monkeypatch.setattr(tb, "detect_pages", lambda p: 3)
        assert _main(monkeypatch, bad, "--output-dir", tmp_path / "out") == 0
        assert flow.extraction

    def test_russian_sample_passes(self, tmp_path, monkeypatch, flow):
        """Kiril metin katmanı (fitz'in yerleşik fontu Kiril basamadığı için
        örnekleyici sahte): K5'ten beri Rusça desteklenir, ön kapı geçer."""
        pdf = _make_pdf(tmp_path / "kniga.pdf", None)
        monkeypatch.setattr(tb, "_pdf_text_sample", lambda p: _repeat("ru"))
        out = tmp_path / "out"
        assert _main(monkeypatch, pdf, "--output-dir", out) == 0
        assert flow.extraction == [str(pdf.resolve())]
        assert not (out / "pipeline.log").exists()

    def test_garbled_ocr_layer_passes(self, tmp_path, monkeypatch, flow):
        pdf = _make_pdf(tmp_path / "scan.pdf", None)
        monkeypatch.setattr(tb, "_pdf_text_sample",
                            lambda p: "\n".join([_GARBLED_CYRILLIC] * 12))
        assert _main(monkeypatch, pdf, "--output-dir", tmp_path / "out") == 0
        assert flow.extraction

    def test_unsupported_epub_stops(self, tmp_path, monkeypatch, flow):
        epub = _make_epub(tmp_path / "livro.epub", "pt")
        assert _main(monkeypatch, epub, "--output-dir", tmp_path / "out",
                     "--no-epub-output") == tb.EXIT_SOURCE_UNSUPPORTED
        assert flow.epub_extraction == []

    def test_german_epub_passes(self, tmp_path, monkeypatch, flow):
        # K5: eskiden Almanca EPUB burada çıkış 4 alıyordu.
        epub = _make_epub(tmp_path / "buch.epub", "de")
        assert _main(monkeypatch, epub, "--output-dir", tmp_path / "out",
                     "--no-epub-output") == 0
        assert flow.epub_extraction

    def test_english_epub_passes(self, tmp_path, monkeypatch, flow):
        epub = _make_epub(tmp_path / "book.epub", "en")
        assert _main(monkeypatch, epub, "--output-dir", tmp_path / "out",
                     "--no-epub-output") == 0
        assert flow.epub_extraction

    def test_epub_sample_skips_head_and_is_bounded(self, tmp_path):
        epub = _make_epub(tmp_path / "buch.epub", "de", chapters=40)
        text = tb._epub_text_sample(str(epub))
        assert "Head English title" not in text
        assert "Die Messung" in text
        assert len(text) <= ld.PREGATE_MAX_CHARS + ld.PREGATE_MAX_PAGES

    def test_pdf_sample_is_bounded_and_spread(self, tmp_path):
        pdf = _make_pdf(tmp_path / "long.pdf", "en", pages=45, per_page=6)
        text = tb._pdf_text_sample(str(pdf))
        per_page = ld.PREGATE_MAX_CHARS // ld.PREGATE_MAX_PAGES
        assert len(text) <= ld.PREGATE_MAX_CHARS + ld.PREGATE_MAX_PAGES
        assert text.count("The temperature") >= ld.PREGATE_MAX_PAGES
        assert per_page < len("\n\n".join([_PARAS["en"]] * 6))


class TestPregateNeverOnYolA:
    """Ön kapı YALNIZ yerel model yolunda: Yol A ve bayraklar dokunmaz."""

    @pytest.fixture
    def spy(self, monkeypatch):
        calls = []

        def _spy(*args, **kwargs):
            calls.append(args)
            return tb.EXIT_SOURCE_NOT_ENGLISH

        monkeypatch.setattr(tb, "_source_language_pregate", _spy)
        return calls

    def test_export_tex(self, tmp_path, monkeypatch, flow, spy):
        pdf = _make_pdf(tmp_path / "buch.pdf", "de")
        assert _main(monkeypatch, pdf, "--output-dir", tmp_path / "out",
                     "--export-tex") == 0
        assert spy == []
        assert flow.extraction, "Yol A çıkarımı çalışmalı"

    def test_import_tex(self, tmp_path, monkeypatch, flow, spy):
        pdf = _make_pdf(tmp_path / "buch.pdf", "de")
        tr = tmp_path / "EXPORT_TR.tex"
        tr.write_text("\\chapter{Giriş}\n" + "Türkçe metin. " * 20,
                      encoding="utf-8")
        assert _main(monkeypatch, pdf, "--output-dir", tmp_path / "out",
                     "--import-tex", tr) == 0
        assert spy == []

    @pytest.mark.parametrize("flag", ["--dil-kontrolu-yok", "--extract-only",
                                      "--resume", "--skip-extract",
                                      "--skip-translate"])
    def test_flags_skip_pregate(self, tmp_path, monkeypatch, flow, spy, flag):
        pdf = _make_pdf(tmp_path / "buch.pdf", "de")
        assert _main(monkeypatch, pdf, "--output-dir", tmp_path / "out",
                     flag) == 0
        assert spy == []

    def test_yol_a_marker_keeps_lock_priority(self, tmp_path, monkeypatch,
                                              flow, spy):
        pdf = _make_pdf(tmp_path / "buch.pdf", "de")
        out = tmp_path / "out"
        (out / "source").mkdir(parents=True)
        (out / "source" / "MANUAL_TRANSLATION.marker").write_text(
            "import: test\n", encoding="utf-8")
        # Yol A kilidi artık çıkarımdan (ve ön kapıdan) ÖNCE denetleniyor:
        # kilitli klasörde koşu hiçbir dosyaya dokunmadan çıkış 2 ile durur.
        assert _main(monkeypatch, pdf, "--output-dir", out) == tb.EXIT_MANUAL_LOCK
        assert spy == []
        # --force-mt ile MT gerçekten koşacağı için ön kapı uygulanır.
        assert _main(monkeypatch, pdf, "--output-dir", out,
                     "--force-mt") == tb.EXIT_SOURCE_NOT_ENGLISH
        assert len(spy) == 1

    def test_1_cikar_never_calls_pregate(self, tmp_path, monkeypatch, spy):
        cikar = _load_cikar()
        pdf = _make_pdf(tmp_path / "buch.pdf", "de")
        monkeypatch.setattr(cikar, "PROJECT_ROOT", tmp_path)
        monkeypatch.setattr(cikar, "run_extraction",
                            lambda *a, **k: True)

        def _pipeline(output_dir, book_name, **kwargs):
            assert kwargs["skip_translate"] is True
            src = Path(output_dir) / "source"
            src.mkdir(parents=True, exist_ok=True)
            (src / "body.tex").write_text(
                "\\chapter{Einleitung}\n" + _PARAS["de"], encoding="utf-8")
            return 0

        monkeypatch.setattr(cikar, "run_pipeline", _pipeline)
        monkeypatch.setattr(sys, "argv", ["1_cikar.py", str(pdf)])
        assert cikar.main() == 0
        assert spy == []
        assert (tmp_path / "output" / "buch" / "EXPORT.tex").exists()

    def test_yol_a_scripts_do_not_reference_pregate(self):
        for script in ("1_cikar.py", "2_derle.py"):
            src = (PROJECT_ROOT / script).read_text(encoding="utf-8")
            assert "pregate" not in src, script


class TestPregateBatch:
    def test_batch_marks_unsupported_failed_without_extraction(
            self, tmp_path, monkeypatch, flow):
        # K5: desteklenmeyen dil (Portekizce) başarısız, Almanca ve İngilizce
        # kitap çıkarıma girer.
        monkeypatch.setattr(tb, "PROJECT_ROOT", tmp_path)
        pt = _make_pdf(tmp_path / "livro.pdf", "pt")
        de = _make_pdf(tmp_path / "buch.pdf", "de")
        en = _make_pdf(tmp_path / "book.pdf", "en")
        monkeypatch.setattr(sys, "argv", ["translate_book.py", str(pt),
                                          "--batch", str(pt), str(de), str(en)])
        args = tb.parse_args()
        results = tb.run_batch([str(pt), str(de), str(en)], args)
        assert [r[:2] for r in results["failed"]] == [
            (str(pt.resolve()), f"exit={tb.EXIT_SOURCE_UNSUPPORTED}")]
        assert flow.extraction == [str(de.resolve()), str(en.resolve())]
        assert [p for p, _ in results["success"]] == [str(de.resolve()),
                                                      str(en.resolve())]

    def test_batch_forwards_kaynak_dil(self, tmp_path, monkeypatch, flow):
        monkeypatch.setattr(tb, "PROJECT_ROOT", tmp_path)
        de = _make_pdf(tmp_path / "buch.pdf", "de")
        monkeypatch.setattr(sys, "argv", ["translate_book.py", str(de),
                                          "--batch", str(de),
                                          "--kaynak-dil", "de"])
        args = tb.parse_args()
        tb.run_batch([str(de)], args)
        assert flow.pipeline[0]["source_lang"] == "de"


# ===========================================================================
# F4 — varsayılan motor tek kaynaktan
# ===========================================================================

class TestDefaultEngineSingleSource:
    def test_no_engine_literal_in_translate_book(self):
        src = (PROJECT_ROOT / "translate_book.py").read_text(encoding="utf-8")
        assert '"hymt-7b"' not in src and "'hymt-7b'" not in src

    def test_parse_args_follows_registry(self, monkeypatch, capsys):
        monkeypatch.setattr(registry, "DEFAULT_ENGINE", "deneme-motor")
        monkeypatch.setattr(sys, "argv", ["translate_book.py", "kitap.pdf"])
        assert tb.parse_args().mt_engine == "deneme-motor"
        monkeypatch.setattr(sys, "argv", ["translate_book.py", "--help"])
        with pytest.raises(SystemExit):
            tb.parse_args()
        assert "varsayilan: deneme-motor" in capsys.readouterr().out

    def test_signature_defaults_are_registry_default(self):
        for fn in (tb.run_pipeline, tb._preflight_check):
            default = inspect.signature(fn).parameters["mt_engine"].default
            assert default == registry.DEFAULT_ENGINE, fn.__name__

    def test_main_fallback_uses_registry(self, tmp_path, monkeypatch, flow):
        monkeypatch.setattr(registry, "DEFAULT_ENGINE", "deneme-motor")
        pdf = _make_pdf(tmp_path / "book.pdf", "en", pages=1)
        monkeypatch.setattr(sys, "argv", [
            "translate_book.py", str(pdf), "--skip-extract",
            "--output-dir", str(tmp_path / "out")])
        args = tb.parse_args()
        del args.mt_engine
        monkeypatch.setattr(tb, "parse_args", lambda: args)
        with pytest.raises(SystemExit):
            tb.main()
        assert flow.pipeline[0]["mt_engine"] == "deneme-motor"

    def test_batch_fallback_uses_registry(self, tmp_path, monkeypatch, flow):
        monkeypatch.setattr(registry, "DEFAULT_ENGINE", "deneme-motor")
        monkeypatch.setattr(tb, "PROJECT_ROOT", tmp_path)
        pdf = _make_pdf(tmp_path / "book.pdf", "en", pages=1)
        monkeypatch.setattr(sys, "argv", ["translate_book.py", str(pdf),
                                          "--batch", str(pdf)])
        args = tb.parse_args()
        del args.mt_engine
        tb.run_batch([str(pdf)], args)
        assert flow.pipeline[0]["mt_engine"] == "deneme-motor"
