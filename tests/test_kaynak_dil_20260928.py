"""Yerel model yolunun kaynak dili: 6 dil (K5, 28 Eylül 2026; KARARLAR 14).

"Yerel model ile çevir" (translate_book.py) artık core.lang_detect.TEX_SOURCE_LANGS
dillerini (en, de, ru, fr, es, it) kaynak kabul eder; hedef dil yine yalnız
Türkçe. "Bozmamaya çalışalım": İngilizce kaynaklı yol davranışça BİREBİR aynı
kalır — bunu İngilizce kilit testi (``TestIngilizceKilit``) denetler:

    Sahte motorlarla (sahte Ollama, sahte OpenAI uyumlu sunucu, MLX yolu için
    sahte ``mlx_lm``) İngilizce sentetik bir gövde translate_book.run_pipeline
    zincirinden geçer; modele giden istek gövdeleri / istemler ve üretilen
    çıktı ``tests/fixtures/yolb_en_kilit_20260928.json`` ile BİREBİR
    karşılaştırılır. Kilit dosyası K5 değişikliklerinden ÖNCEKİ kodla üretildi
    (``KITAPCEVIR_KILIT_URET=1`` ile yeniden üretilebilir; yalnız bilinçli bir
    İngilizce davranış değişikliğinde).

    Bilinçli değişiklik kaydı: 28 Eylül 2026 (Vson, Vk bulgusu 2) — yer
    tutucu geri yazımı TeX bağından (``~``) sonra boşluk eklemiyor
    (``Section~ \\ref{x}`` -> ``Section~\\ref{x}``). Kilit yeniden üretildi;
    fark YALNIZ üç motorun ``translated_body``'sindeki bu tek bağdır, istek
    gövdeleri / istemler ve retranslator çıktısı aynı kaldı (önceki kilit iç
    denetim arşivinde).

Model YÜKLENMEZ, gerçek Ollama / LM Studio'ya gidilmez; çeviri belleği geçici
SQLite'tır (data/translation_memory*.db'ye dokunulmaz). Test girdileri
sentetiktir (kitap metni kopyalanmadı).
"""

from __future__ import annotations

import json
import os
import re
import sys
import types
import zlib
from pathlib import Path

import pytest

import translate_book as tb
from core.mt_engines import discovery
from core.mt_engines.contracts import OpenAIServer, SourcesConfig

PROJECT_ROOT = Path(__file__).resolve().parent.parent
LOCK_PATH = PROJECT_ROOT / "tests" / "fixtures" / "yolb_en_kilit_20260928.json"
LOCK_REGEN_ENV = "KITAPCEVIR_KILIT_URET"

OLLAMA_MODEL = "sahte"
OPENAI_MODEL = "qwen"

# ---------------------------------------------------------------------------
# Sahte "çevirmen": sözcük başına kararlı Türkçe sözcük (crc32), yer tutucular
# ve sayılar AYNEN. Türkçeye özgü harfli sözcükler seçildi: çıktı hiçbir dil
# kapısında "çevrilmemiş" sayılmasın.
# ---------------------------------------------------------------------------
_TR_WORDS = (
    "çalışma", "değer", "ışık", "yörünge", "şekil", "güneş", "ağırlık",
    "kütle", "hız", "yüzey", "dönüş", "öğrenci", "kuvvet", "enerji",
    "sıcaklık", "basınç", "akış", "düzlem", "eğri", "açı",
)
_PLACEHOLDER_SPLIT_RE = re.compile(r"(<<[A-Z]+_\d+>>|\[\[[A-Za-z]+_\d+\]\]|@@[A-Za-z]+\d+@@)")
_LETTER_RUN_RE = re.compile(r"[^\W\d_]+")


def fake_turkish(text: str) -> str:
    """Kaynak metni kararlı biçimde 'Türkçeye çevir' (yalnız test altyapısı)."""
    def word(m: re.Match) -> str:
        src = m.group(0)
        tr = _TR_WORDS[zlib.crc32(src.lower().encode("utf-8")) % len(_TR_WORDS)]
        return tr[:1].upper() + tr[1:] if src[:1].isupper() else tr

    parts = []
    for part in _PLACEHOLDER_SPLIT_RE.split(text):
        if _PLACEHOLDER_SPLIT_RE.fullmatch(part):
            parts.append(part)
        else:
            parts.append(_LETTER_RUN_RE.sub(word, part))
    return "".join(parts)


# ---------------------------------------------------------------------------
# İngilizce sentetik gövde (başlık, paragraf, matematik, yer tutucu, dipnot,
# atıf, tablo, Latince alıntı, sözlük başlığı)
# ---------------------------------------------------------------------------
EN_BODY = (
    "\\chapter{Orbital Motion}\\label{ch:orbit}\n\n"
    "\\section{Introduction}\n\n"
    "The satellite moves along an elliptical path around the planet. Its speed "
    "changes with the distance $r$ from the focus, as shown by the relation "
    "$v^2 = GM(2/r - 1/a)$.\\footnote{This relation is known as the vis-viva "
    "equation.}\n\n"
    "\\section{Energy of the Orbit}\\label{sec:energy}\n\n"
    "We refer to Section~\\ref{ch:orbit} and cite \\cite{kepler1609} for the "
    "historical background of the problem, as shown in Figure 2.\n\n"
    "\\begin{equation}\n"
    "E = -\\frac{GMm}{2a}\n"
    "\\label{eq:energy}\n"
    "\\end{equation}\n\n"
    "The total energy depends only on the semi-major axis of the ellipse, which "
    "is a remarkable result for any student of mechanics.\n\n"
    "\\begin{tabular}{ll}\n"
    "Quantity & Value \\\\\n"
    "Mass of the planet & very large \\\\\n"
    "\\end{tabular}\n\n"
    "Omnis motus corporum est in tempore et quod semper idem manet.\n\n"
    "\\subsection{Circular Orbits}\n\n"
    "A circular orbit is a special case of the ellipse in which the distance "
    "from the focus never changes during the motion of the body.\n"
)

# Retranslator (Phase 5.7) girdisi: Türkçe gövdede İngilizce kalmış satırlar.
EN_RETRANSLATE_BODY = (
    "\\section{Yörünge Hareketi}\n\n"
    "Uydu gezegenin çevresinde eliptik bir yolda hareket eder ve hızı değişir.\n\n"
    "The total energy of the satellite depends only on the size of the orbit.\n\n"
    "\\subsection{The Shape of the Orbit}\n\n"
    "Bu bölümde yörüngenin biçimi incelenir; ayrıntılar ekte verilmiştir.\n"
)


# ---------------------------------------------------------------------------
# Ortak kurulum
# ---------------------------------------------------------------------------
@pytest.fixture
def restore_root_logger():
    """run_pipeline kök logger'ı ele geçirir; test sonrası eski hâle döndür."""
    import logging

    root = logging.getLogger()
    old_handlers = list(root.handlers)
    old_level = root.level
    yield
    for h in list(root.handlers):
        if h not in old_handlers:
            try:
                h.close()
            except Exception:
                pass
    root.handlers[:] = old_handlers
    root.setLevel(old_level)


@pytest.fixture
def pipeline_env(monkeypatch, tmp_path, restore_root_logger):
    """run_pipeline'ı modelsiz koşturmaya hazırla: ön kontrol geçer, bellek geçici."""
    from core.translator import BookTranslator

    monkeypatch.setattr(tb, "_preflight_check", lambda *a, **k: [])
    memory_db = tmp_path / "bellek.db"
    real_init_memory = BookTranslator._init_memory

    def _tmp_memory(self):
        self.config.memory_db_path = str(memory_db)
        real_init_memory(self)

    monkeypatch.setattr(BookTranslator, "_init_memory", _tmp_memory)
    return tmp_path


def _point_sources(monkeypatch, *, ollama_url: str = "http://127.0.0.1:9",
                   openai_servers=()) -> None:
    sources = SourcesConfig(ollama_url=ollama_url,
                            lmstudio_url="http://127.0.0.1:9/v1",
                            openai_servers=tuple(openai_servers))
    monkeypatch.setattr(discovery, "load_sources", lambda path=None: sources)


def make_book(root: Path, body: str, name: str = "kitap") -> Path:
    out = root / name
    (out / "source").mkdir(parents=True)
    (out / "full_book.md").write_text("# Kitap\n\n" + body, encoding="utf-8")
    (out / "source" / "body.tex").write_text(body, encoding="utf-8")
    return out


def run_book(out: Path, mt_engine: str, **kwargs) -> int:
    # resume=True + body.tex -> Phase 2 (dönüştürücü) atlanır, gövde aynen
    # çeviriye gider; skip_qc -> Phase 5.5–5.8 (başka kulvarların QC'si) yok.
    params = dict(skip_translate=False, skip_compile=True, skip_qc=True,
                  book_type="science", pdf_path=None, resume=True,
                  mt_engine=mt_engine)
    params.update(kwargs)
    return tb.run_pipeline(str(out), "Kilit Kitap", **params)


def _translated(out: Path) -> str:
    return (out / "source" / "translated_body.tex").read_text(encoding="utf-8")


def _server_requests(srv) -> list:
    return [{"method": r.method, "path": r.path, "json": r.json}
            for r in srv.request_details]


class _FakeMlxModule(types.ModuleType):
    """``mlx_lm`` sahtesi: ``load`` + ``generate`` (``batch_generate`` YOK ->
    sıralı üretim yolu). İstemleri kaydeder, HY-MT istem biçiminden metni
    çıkarıp sahte Türkçe döndürür."""

    def __init__(self):
        super().__init__("mlx_lm")
        self.prompts: list = []

        def load(path):
            return object(), object()

        def generate(model, tokenizer, prompt="", max_tokens=0, **kwargs):
            self.prompts.append({"prompt": prompt, "max_tokens": max_tokens})
            text = prompt.split("<|extra_4|>", 1)[-1].split("<|extra_0|>", 1)[0]
            return fake_turkish(text)

        self.load = load
        self.generate = generate


@pytest.fixture
def fake_mlx(monkeypatch, tmp_path):
    """Varsayılan motor (HY-MT, MLX) için sahte ``mlx_lm`` + sahte model klasörü."""
    from core.mt_engines.hymt import HYMTFormatEngine

    mod = _FakeMlxModule()
    monkeypatch.setitem(sys.modules, "mlx_lm", mod)
    model_dir = tmp_path / "sahte-mlx"
    model_dir.mkdir()
    monkeypatch.setattr(HYMTFormatEngine, "resolve_local_model_dir",
                        lambda self, models_dir: model_dir)
    import platform
    monkeypatch.setattr(platform, "system", lambda: "Darwin")
    return mod


def _retranslate(mt_engine: str, body: str) -> dict:
    """Phase 5.7'nin eşdeğeri: çevirmen köprüsü + başlık otoritesiyle yeniden çeviri."""
    from latex_pipeline.config import PipelineConfig
    from latex_pipeline.retranslator import scan_and_retranslate
    from latex_pipeline.translator import LatexTranslator

    lt = LatexTranslator(pipeline_config=PipelineConfig(translate=True),
                         mt_engine=mt_engine, source_lang="en",
                         target_lang="tr", book_type="science",
                         project_name="Kilit Kitap")
    res = scan_and_retranslate(body, translator_fn=lt._safe_translate,
                               heading_translator_fn=lt._translate_heading_text)
    return {"output": res.output_tex,
            "english_lines_found": res.english_lines_found,
            "retranslated_lines": res.retranslated_lines}


def collect_ollama(fake_llm_server, monkeypatch, root: Path) -> dict:
    srv = fake_llm_server("ollama", models=[OLLAMA_MODEL], reply=fake_turkish)
    _point_sources(monkeypatch, ollama_url=srv.url)
    engine = f"ollama:{OLLAMA_MODEL}"
    out = make_book(root, EN_BODY, "kitap_ollama")
    code = run_book(out, engine)
    run_requests = _server_requests(srv)
    srv.reset_requests()
    rt = _retranslate(engine, EN_RETRANSLATE_BODY)
    rt["requests"] = _server_requests(srv)
    return {"exit_code": code, "requests": run_requests,
            "translated_body": _translated(out), "retranslator": rt}


def collect_openai(fake_llm_server, monkeypatch, root: Path) -> dict:
    srv = fake_llm_server("openai", models=[OPENAI_MODEL], reply=fake_turkish)
    _point_sources(monkeypatch, openai_servers=[
        OpenAIServer(name="sahte", url=srv.base_url)])
    engine = f"openai:{srv.base_url}|{OPENAI_MODEL}"
    out = make_book(root, EN_BODY, "kitap_openai")
    code = run_book(out, engine)
    run_requests = _server_requests(srv)
    srv.reset_requests()
    rt = _retranslate(engine, EN_RETRANSLATE_BODY)
    rt["requests"] = _server_requests(srv)
    return {"exit_code": code, "requests": run_requests,
            "translated_body": _translated(out), "retranslator": rt}


def collect_mlx(fake_mlx, root: Path) -> dict:
    from core.mt_engines import registry

    out = make_book(root, EN_BODY, "kitap_mlx")
    code = run_book(out, registry.DEFAULT_ENGINE)
    prompts = list(fake_mlx.prompts)
    fake_mlx.prompts.clear()
    rt = _retranslate(registry.DEFAULT_ENGINE, EN_RETRANSLATE_BODY)
    rt["prompts"] = list(fake_mlx.prompts)
    return {"exit_code": code, "prompts": prompts,
            "translated_body": _translated(out), "retranslator": rt}


def _normalize(obj):
    """Sunucu adresi/portu gibi koşudan koşuya değişen parçaları sabitle."""
    text = json.dumps(obj, ensure_ascii=False, sort_keys=True)
    text = re.sub(r"http://127\.0\.0\.1:\d+", "http://127.0.0.1:PORT", text)
    return json.loads(text)


def _check_or_write(key: str, value: dict) -> None:
    value = _normalize(value)
    if os.environ.get(LOCK_REGEN_ENV) == "1":
        data = {}
        if LOCK_PATH.exists():
            data = json.loads(LOCK_PATH.read_text(encoding="utf-8"))
        data[key] = value
        LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
        LOCK_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=1,
                                        sort_keys=True) + "\n", encoding="utf-8")
        pytest.skip(f"kilit üretildi: {key}")
    expected = json.loads(LOCK_PATH.read_text(encoding="utf-8"))[key]
    assert value == expected


# ===========================================================================
# (2) İngilizce kilit testi — K5 değişikliklerinden ÖNCE üretildi
# ===========================================================================
class TestIngilizceKilit:
    """İngilizce kaynaklı yerel model yolu: istekler ve çıktı BİREBİR aynı."""

    def test_ollama_istekleri_ve_cikti_ayni(self, fake_llm_server, monkeypatch,
                                            pipeline_env):
        got = collect_ollama(fake_llm_server, monkeypatch, pipeline_env)
        assert got["exit_code"] == 0
        assert got["requests"], "modele hiç istek gitmedi"
        _check_or_write("ollama", got)

    def test_openai_istekleri_ve_cikti_ayni(self, fake_llm_server, monkeypatch,
                                            pipeline_env):
        got = collect_openai(fake_llm_server, monkeypatch, pipeline_env)
        assert got["exit_code"] == 0
        assert got["requests"], "modele hiç istek gitmedi"
        _check_or_write("openai", got)

    def test_mlx_istemleri_ve_cikti_ayni(self, fake_mlx, pipeline_env):
        got = collect_mlx(fake_mlx, pipeline_env)
        assert got["exit_code"] == 0
        assert got["prompts"], "MLX'e hiç istem gitmedi"
        _check_or_write("mlx", got)


# ===========================================================================
# Sentetik İngilizce DIŞI belgeler (bu test için yazıldı; kitap düzyazısı değil)
# ===========================================================================
_DE_PARAS = (
    "Ein Planet bewegt sich auf einer elliptischen Bahn um die Sonne. Seine "
    "Geschwindigkeit hängt von dem Abstand $r$ zum Brennpunkt ab, wie die "
    "Gleichung $v^2 = GM(2/r - 1/a)$ zeigt.\\footnote{Diese Beziehung wird "
    "auch die Energiegleichung der Bahn genannt.}",
    "Wir verweisen auf Abschnitt~\\ref{ch:planet} und zitieren "
    "\\cite{kepler1609} für die Geschichte des Problems, denn die ersten "
    "Messungen waren sehr ungenau und wurden später oft wiederholt.",
    "Die Gesamtenergie hängt nur von der großen Halbachse der Ellipse ab, "
    "und das ist ein bemerkenswertes Ergebnis für jeden Studenten der "
    "Mechanik, der die Bewegung der Körper verstehen will.",
    "Eine Kreisbahn ist ein besonderer Fall der Ellipse, bei dem sich der "
    "Abstand zum Brennpunkt während der Bewegung des Körpers nicht ändert "
    "und die Geschwindigkeit immer gleich bleibt.",
    "Wenn die Energie negativ ist, bleibt der Körper an die Sonne gebunden; "
    "wenn sie positiv ist, verlässt er das System auf einer offenen Bahn "
    "und kehrt nie mehr zurück.",
)
# Diesen Absatz "übersetzt" das Modell NIE (Echo): Die Prüfung muss ihn als
# unübersetzt in der Quellsprache finden.
_DE_ECHO = ("Dieser Absatz bleibt in der Ausgabe unverändert, weil das Modell "
            "ihn nicht übersetzt und die Prüfung ihn in der Quellsprache finden "
            "muss.")
DE_BODY = (
    "\\chapter{Bewegung der Planeten}\\label{ch:planet}\n\n"
    "\\section{Die Bahn und die Energie}\n\n"
    + "\n\n".join(_DE_PARAS) + "\n\n"
    "\\begin{equation}\nE = -\\frac{GMm}{2a}\n\\label{eq:energie}\n"
    "\\end{equation}\n\n"
    + _DE_ECHO + "\n\n"
    + "\n\n".join(p.replace("Sonne", "Erde") for p in _DE_PARAS[2:]) + "\n"
)

_RU_PARAS = (
    "Планета движется по эллиптической орбите вокруг Солнца. Её скорость "
    "зависит от расстояния $r$ до фокуса, как показывает уравнение "
    "$v^2 = GM(2/r - 1/a)$.\\footnote{Это соотношение также называют "
    "уравнением энергии орбиты.}",
    "Мы ссылаемся на раздел~\\ref{ch:planet} и цитируем \\cite{kepler1609} "
    "для истории задачи, потому что первые измерения были очень неточными "
    "и потом их часто повторяли.",
    "Полная энергия зависит только от большой полуоси эллипса, и это "
    "замечательный результат для каждого студента механики, который хочет "
    "понять движение тел.",
    "Круговая орбита является особым случаем эллипса, при котором "
    "расстояние до фокуса не меняется во время движения тела и скорость "
    "всегда остаётся одинаковой.",
    "Если энергия отрицательна, тело остаётся связанным с Солнцем; если она "
    "положительна, тело покидает систему по открытой орбите и больше не "
    "возвращается.",
)
_RU_ECHO = ("Этот абзац остаётся в выводе без изменений, потому что модель его "
            "не переводит и проверка должна найти его на исходном языке.")
RU_BODY = (
    "\\chapter{Движение планет}\\label{ch:planet}\n\n"
    "\\section{Орбита и энергия}\n\n"
    + "\n\n".join(_RU_PARAS) + "\n\n"
    "\\begin{equation}\nE = -\\frac{GMm}{2a}\n\\label{eq:energie}\n"
    "\\end{equation}\n\n"
    + _RU_ECHO + "\n\n"
    + "\n\n".join(p.replace("Солнцем", "Землёй") for p in _RU_PARAS[2:]) + "\n"
)

_ZH_PARA = ("行星沿着椭圆轨道绕太阳运动，它的速度取决于到焦点的距离，"
            "总能量只取决于椭圆的半长轴。")
ZH_BODY = "\\chapter{行星}\n\n" + "\n\n".join([_ZH_PARA] * 30) + "\n"

_ECHO_MARKERS = ("unverändert", "без изменений")


def fake_turkish_with_echo(text: str) -> str:
    """Sahte çevirmen: işaretli paragrafı çevirmez (yankı), gerisini çevirir."""
    if any(marker in text for marker in _ECHO_MARKERS):
        return text
    return fake_turkish(text)


def _log(out: Path) -> str:
    return (out / "pipeline.log").read_text(encoding="utf-8")


def _chat_texts(srv) -> list:
    return [b["messages"][-1]["content"] for b in srv.requests_to("/api/chat")]


# ===========================================================================
# (3) Dil kapısı: karar tablosu, çıkış kodu, öncelik, --kaynak-dil
# ===========================================================================
class TestDilKapisi:
    @pytest.fixture
    def estimate(self, monkeypatch):
        from core import lang_detect as ld

        def _set(code, conf, letters=5000, words=800, coverage=0.3):
            monkeypatch.setattr(ld, "analyze_language",
                                lambda *a, **k: ld.LanguageEstimate(
                                    code, conf, letters=letters, words=words,
                                    coverage=coverage))
        return _set

    @pytest.mark.parametrize("code,conf,expected", [
        ("en", 0.99, (None, "en")),
        ("en", 0.30, (None, "en")),          # İngilizce güvenden bağımsız (eskisi gibi)
        ("de", 0.99, (None, "de")),
        ("ru", 0.99, (None, "ru")),
        ("fr", 0.80, (None, "fr")),          # eşik dahil
        ("es", 0.95, (None, "es")),
        ("it", 0.95, (None, "it")),
        ("de", 0.79, (None, "en")),          # düşük güven: İngilizce varsayımı
        ("und", 0.0, (None, "en")),
        ("cjk", 0.99, (4, "en")),            # desteklenmeyen dil
        ("pt", 0.95, (4, "en")),
        ("cyrillic", 0.95, (4, "en")),       # Rusça olmayan Kiril
        ("tr", 0.95, (4, "en")),             # hedef dil kaynak olamaz
        ("pt", 0.70, (None, "en")),          # belirsiz: durdurmaz
    ])
    def test_karar_tablosu(self, estimate, code, conf, expected):
        import logging

        estimate(code, conf)
        assert tb._source_language_decision(
            "metin", logging.getLogger("k5.karar")) == expected
        assert tb._source_language_gate(
            "metin", logging.getLogger("k5.karar")) == expected[0]

    def test_elle_dil_olcmeden_gecer(self, monkeypatch):
        import logging

        from core import lang_detect as ld

        def _boom(*a, **k):
            raise AssertionError("elle dil verilince ölçüm yapılmamalı")

        monkeypatch.setattr(ld, "analyze_language", _boom)
        log = logging.getLogger("k5.elle")
        assert tb._source_language_decision("metin", log, forced="es") == (None, "es")
        assert tb._source_language_decision("metin", log, check=False) == (None, "en")

    def test_resume_yolu_durdurmaz(self, estimate):
        import logging

        estimate("cjk", 0.99)
        assert tb._source_language_decision(
            "metin", logging.getLogger("k5.r"), stop=False) == (None, "en")

    def test_cikis_kodu_adlari(self):
        from ui import runner

        assert tb.EXIT_SOURCE_UNSUPPORTED == tb.EXIT_SOURCE_NOT_ENGLISH == 4
        assert runner.EXIT_SOURCE_UNSUPPORTED == runner.EXIT_SOURCE_NOT_ENGLISH == 4
        assert tb.EXIT_SOURCE_UNSUPPORTED not in (
            0, 1, tb.EXIT_MANUAL_LOCK, 3, tb.EXIT_MODEL_UNAVAILABLE, 130)

    def test_lang_detect_kararlari_listeyi_izler(self):
        from core import lang_detect as ld

        for code in ld.TEX_SOURCE_LANGS:
            est = ld.LanguageEstimate(code, 1.0, letters=10 ** 5, coverage=1.0)
            assert not ld.should_stop_for_source_language(est), code
            assert not ld.should_stop_before_extraction(est), code
        est = ld.LanguageEstimate("pt", 1.0, letters=10 ** 5, coverage=1.0)
        assert ld.should_stop_for_source_language(est)
        assert ld.should_stop_before_extraction(est)
        # Liste parametreyle daraltılırsa (eski davranış) Almanca durur.
        est = ld.LanguageEstimate("de", 1.0, letters=10 ** 5, coverage=1.0)
        assert ld.should_stop_for_source_language(est, ("en",))

    def test_kaynak_dil_secenekleri_tek_kaynaktan(self, monkeypatch):
        from core.lang_detect import TEX_SOURCE_LANGS

        for code in TEX_SOURCE_LANGS:
            monkeypatch.setattr(sys, "argv", ["translate_book.py", "k.pdf",
                                              "--kaynak-dil", code])
            assert tb.parse_args().kaynak_dil == code
        monkeypatch.setattr(sys, "argv", ["translate_book.py", "k.pdf"])
        assert tb.parse_args().kaynak_dil is None

    def test_oncelik_dil_modelden_once(self, tmp_path, monkeypatch):
        """Desteklenmeyen dil (ön kapı) modelin denetiminden ÖNCE durur (4, 5 değil)."""
        calls = []
        monkeypatch.setattr(tb, "_pdf_text_sample",
                            lambda p: " ".join(["palavra"] * 10))
        from core import lang_detect as ld
        monkeypatch.setattr(ld, "analyze_language",
                            lambda *a, **k: ld.LanguageEstimate(
                                "pt", 0.99, letters=10 ** 5, coverage=0.5))
        monkeypatch.setattr(tb, "_check_engine",
                            lambda e: calls.append(e) or None)
        monkeypatch.setattr(tb, "run_extraction",
                            lambda *a, **k: calls.append("cikarim") or True)
        monkeypatch.delenv("KITAPCEVIR_EVENT_LOG", raising=False)
        pdf = tmp_path / "livro.pdf"
        pdf.write_bytes(b"%PDF-1.4\n" + b"x" * 2048)
        monkeypatch.setattr(sys, "argv", [
            "translate_book.py", str(pdf), "--output-dir", str(tmp_path / "out"),
            "--mt-engine", "ollama:olmayan"])
        with pytest.raises(SystemExit) as exc:
            tb.main()
        assert exc.value.code == tb.EXIT_SOURCE_UNSUPPORTED
        assert calls == []

    def test_elle_dil_zincire_akar(self, pipeline_env, monkeypatch):
        """--kaynak-dil: ölçüm kararı bağlamaz, dil LatexTranslator'a akar."""
        seen = []

        class _Spy:
            def __init__(self, **kwargs):
                seen.append(kwargs)
                self.stats = types.SimpleNamespace(
                    segments_total=1, segments_translated=1,
                    segments_failed=0, segments_skipped=0)

            def translate(self, body, checkpoint_dir=None, resume=False,
                          progress_callback=None):
                return fake_turkish(body)

        monkeypatch.setattr("latex_pipeline.translator.LatexTranslator", _Spy)
        out = make_book(pipeline_env, EN_BODY, "elle")
        assert run_book(out, "hymt-7b", source_lang="de") == 0
        assert [k["source_lang"] for k in seen] == ["de"]
        log = _log(out)
        assert "Kaynak dil elle verildi (--kaynak-dil): Almanca" in log
        assert "Kaynak dil kapısı:" not in log
        assert "Phase 5: MT Translation (DE -> TR" in log

    def test_resume_cevrilmis_govdede_dil_akar(self, pipeline_env, monkeypatch):
        """Phase 5.5'ten resume: çevirmen (5.7/5.8 için) ölçülen dille kurulur."""
        seen = []

        class _Spy:
            def __init__(self, **kwargs):
                seen.append(kwargs)

        monkeypatch.setattr("latex_pipeline.translator.LatexTranslator", _Spy)
        out = make_book(pipeline_env, DE_BODY, "resume")
        (out / "source" / "translated_body.tex").write_text(
            fake_turkish(DE_BODY), encoding="utf-8")
        assert run_book(out, "hymt-7b") == 0
        assert [k["source_lang"] for k in seen] == ["de"]


# ===========================================================================
# (4) İstemler: kaynak dil adı tek yerden, İngilizce aynen, hedef hep Türkçe
# ===========================================================================
class TestIstemler:
    def test_mlx_istemi_almanca(self, fake_mlx, pipeline_env):
        from core.translator import _PLACEHOLDER_CLAUSE

        out = make_book(pipeline_env, DE_BODY, "mlx_de")
        assert run_book(out, "hymt-7b") == 0
        prompts = [p["prompt"] for p in fake_mlx.prompts]
        assert prompts
        heads = {p.split("<|extra_4|>")[0] for p in prompts}
        body = "Translate from German to Turkish. Preserve math notation. "
        assert heads <= {
            "<|startoftext|>" + body + "Output only the translation.",
            "<|startoftext|>" + body + _PLACEHOLDER_CLAUSE
            + "Output only the translation.",
            "<|startoftext|>Translate this heading from German to Turkish. It "
            "is a short title; translate it fully into Turkish. Do not repeat "
            "the German text. Output only the translated heading.",
        }, heads
        assert not any("English" in p for p in prompts)

    def test_sohbet_istemi_rusca(self, fake_llm_server, monkeypatch, pipeline_env):
        srv = fake_llm_server("ollama", models=[OLLAMA_MODEL],
                              reply=fake_turkish_with_echo)
        _point_sources(monkeypatch, ollama_url=srv.url)
        out = make_book(pipeline_env, RU_BODY, "ollama_ru")
        assert run_book(out, f"ollama:{OLLAMA_MODEL}") == 0
        texts = _chat_texts(srv)
        assert texts
        assert all(t.startswith(("Translate from Russian to Turkish.",
                                 "Translate this heading from Russian to Turkish."))
                   for t in texts), texts[:3]

    def test_kullanici_dil_tablosu_eksikse_varsayilan_ad(self):
        from core.mt_engines.base import EngineSpec
        from core.mt_engines.chat import ChatTemplateEngine
        from core.translator import _complete_lang_names

        eng = ChatTemplateEngine(EngineSpec(
            name="deneme", format="chat", languages={"en": "English",
                                                     "tr": "Türkisch"}))
        assert eng.lang_name("de", "English") == "English"   # eski kusur
        _complete_lang_names(eng)
        assert eng.lang_name("de", "English") == "German"
        assert eng.lang_name("ru", "English") == "Russian"
        # Kullanıcı değerleri önceliklidir; İngilizce adı aynen.
        assert eng.lang_name("tr", "Turkish") == "Türkisch"
        assert eng.lang_name("en", "English") == "English"

    def test_tablo_vermeyen_motora_dokunulmaz(self):
        from core.mt_engines.base import EngineSpec
        from core.mt_engines.hymt import HYMTFormatEngine
        from core.translator import _complete_lang_names

        eng = HYMTFormatEngine(EngineSpec(name="hymt-deneme"))
        _complete_lang_names(eng)
        assert eng.spec.languages == {}
        assert eng.lang_name("de", "English") == "German"


# ===========================================================================
# (5) Çevrilmemiş satır tespiti ve yeniden çeviri kaynak dile göre
# ===========================================================================
_EN_SAMPLES = (
    ("The total energy of the satellite depends only on the size of the orbit.", False),
    ("Uydu gezegenin çevresinde eliptik bir yolda hareket eder ve hızı değişir.", False),
    ("\\section{The Shape of the Orbit}", True),
    ("\\section{Yörüngenin Biçimi}", True),
    ("Birisi bunu elde eder ve sonuca ulaşır.", False),
    ("Die Kraft wirkt auf den Körper und die Beschleunigung ist proportional.", False),
    ("Любой летательный аппарат меняет свою скорость только под действием силы.", False),
    ("The results of the experiment are shown in the table below the text.", False),
)


class TestCevrilmemisSatir:
    @pytest.mark.parametrize("text,heading", _EN_SAMPLES)
    def test_ingilizce_birebir_ayni(self, text, heading):
        from latex_pipeline.retranslator import is_english_line, is_untranslated_line

        for kwargs in (dict(), dict(min_words=4, min_length=15),
                       dict(min_words=1, min_length=4)):
            assert (is_untranslated_line(text, "en", is_heading=heading, **kwargs)
                    == is_english_line(text, is_heading=heading, **kwargs))

    @pytest.mark.parametrize("lang,line,expected", [
        ("de", "Die Kraft wirkt auf den Körper und die Beschleunigung ist proportional.", True),
        ("de", "Kuvvet cisme etki eder ve ivme kuvvetle orantılıdır, bu açıktır.", False),
        ("de", "The total energy of the satellite depends only on the size of the orbit.", True),
        ("fr", "La force agit sur le corps et l'accélération est proportionnelle à la force.", True),
        ("es", "La fuerza actúa sobre el cuerpo y la aceleración es proporcional a la fuerza.", True),
        ("it", "La forza agisce sul corpo e l'accelerazione è proporzionale alla forza.", True),
        ("ru", "Сила действует на тело, и ускорение пропорционально этой силе всегда.", True),
        ("ru", "Kuvvet cisme etki eder ve ivme kuvvetle orantılıdır, bu açıktır.", False),
    ])
    def test_kaynak_dilde_kalan_satir(self, lang, line, expected):
        from latex_pipeline.retranslator import is_untranslated_line

        assert is_untranslated_line(line, lang) is expected

    @pytest.mark.parametrize("lang,heading,expected", [
        ("ru", "\\section{Движение планет}", True),
        ("ru", "\\section{Gezegenlerin Hareketi}", False),
        ("de", "\\section{Die Bahn und die Energie}", True),
        ("de", "\\section{Yörünge ve Enerji}", False),
    ])
    def test_baslik(self, lang, heading, expected):
        from latex_pipeline.retranslator import is_untranslated_line

        assert is_untranslated_line(heading, lang, is_heading=True) is expected

    def test_retranslator_kaynak_dilde_arar(self):
        from latex_pipeline.retranslator import scan_and_retranslate

        body = ("\\section{Yörünge}\n\n"
                "Uydu gezegenin çevresinde eliptik bir yolda hareket eder.\n\n"
                + _DE_PARAS[2] + "\n")
        calls = []

        def tr(text):
            calls.append(text)
            return "Toplam enerji yalnız elipsin büyük yarı ekseninden etkilenir."

        res = scan_and_retranslate(body, translator_fn=tr, source_lang="de")
        assert res.english_lines_found == 1 and res.retranslated_lines == 1
        assert "Gesamtenergie" not in res.output_tex
        assert calls and "Gesamtenergie" in calls[0]

    def test_kaynakca_bolgesi_kaynak_dilinde_korunur(self):
        from latex_pipeline.retranslator import scan_and_retranslate

        body = ("\\section*{Literaturverzeichnis}\n\n"
                "Kepler, J. Die neue Astronomie und die Bewegung des Mars, 1609.\n")
        res_de = scan_and_retranslate(body, translator_fn=lambda t: "x",
                                      source_lang="de")
        assert res_de.english_lines_found == 0 and res_de.output_tex == body

    def test_latex_translator_dedektorleri(self):
        from latex_pipeline.config import PipelineConfig
        from latex_pipeline.translator import LatexTranslator

        de = LatexTranslator(PipelineConfig(), source_lang="de")
        en = LatexTranslator(PipelineConfig(), source_lang="en")
        german = _DE_PARAS[2]
        assert de._is_english_sentence(german) is True
        assert de._heading_is_english("Die Bahn und die Energie") is True
        assert de._heading_is_english("Yörünge ve Enerji") is False
        from latex_pipeline.retranslator import is_english_line
        assert en._is_english_sentence(german) == is_english_line(
            german, min_words=4, min_length=15)

    @pytest.mark.parametrize("source,text,expected", [
        ("de", "Omnis motus corporum est in tempore et quod semper idem manet.", "la"),
        ("fr", "Omnis motus corporum est in tempore et quod semper idem manet.", None),
        ("fr", "Il est clair que ce qui se passe ici est tres important pour nous.", None),
        ("de", "Die Kraft wirkt auf den Körper und die Beschleunigung ist groß.", None),
        ("fr", "Любой летательный аппарат меняет свою скорость только тогда.", "ru"),
        ("ru", "Любой летательный аппарат меняет свою скорость только тогда.", None),
        ("ru", "The satellite moves along the elliptical path, and it is visible "
               "from the ground at night.", "en"),
        # İşlev kelimesi az (kanıt yetersiz): karar verilmez, MT'ye gider.
        ("ru", "The satellite moves along an elliptical path around the planet.", None),
        ("ru", "Die Kraft wirkt auf den Körper und die Beschleunigung ist groß.", "de"),
        ("en", "Omnis motus corporum est in tempore et quod semper idem manet.", "la"),
        ("en", "The satellite moves along an elliptical path around the planet.", None),
    ])
    def test_yabanci_segment_kapisi(self, source, text, expected):
        from core.lang_detect import classify_foreign_segment
        from latex_pipeline.config import PipelineConfig
        from latex_pipeline.translator import LatexTranslator

        assert classify_foreign_segment(text, source) == expected
        lt = LatexTranslator(PipelineConfig(), source_lang=source)
        assert lt._foreign_segment_code(text) == expected

    def test_cumle_bolme_kaynak_dile_gore(self):
        from latex_pipeline.config import PipelineConfig
        from latex_pipeline.translator import LatexTranslator

        de = LatexTranslator(PipelineConfig(), source_lang="de")
        ru = LatexTranslator(PipelineConfig(), source_lang="ru")
        en = LatexTranslator(PipelineConfig(), source_lang="en")
        assert en._source_sentence_split_re() is LatexTranslator._SENTENCE_SPLIT_RE
        assert de._source_sentence_split_re().split(
            "Das ist z. B. Ein Test. Im 19. Jahrhundert wurde gebaut. Danach kam mehr.") == [
            "Das ist z. B. Ein Test.", "Im 19. Jahrhundert wurde gebaut.",
            "Danach kam mehr."]
        assert ru._source_sentence_split_re().split(
            "Первое предложение. Второе, т. е. Пример. Третье.") == [
            "Первое предложение.", "Второе, т. е. Пример.", "Третье."]
        # İngilizce desen Kiril büyük harfi tanımıyordu (Rusça hiç bölünmezdi).
        assert len(LatexTranslator._SENTENCE_SPLIT_RE.split(
            "Первое предложение. Второе предложение.")) == 1

    def test_retranslator_cli_kaynak_dil(self, tmp_path, monkeypatch, capsys):
        from latex_pipeline import retranslator

        tex = tmp_path / "kitap" / "source" / "translated_body.tex"
        tex.parent.mkdir(parents=True)
        tex.write_text("Uydu gezegenin çevresinde hareket eder.\n\n"
                       + _DE_PARAS[2] + "\n", encoding="utf-8")
        monkeypatch.setattr(sys, "argv", ["retranslator", str(tex), "--dry-run",
                                          "--kaynak-dil", "de"])
        retranslator._cli_main()
        assert "English detected:   1" in capsys.readouterr().out
        monkeypatch.setattr(sys, "argv", ["retranslator", str(tex), "--dry-run",
                                          "--kaynak-dil", "pt"])
        with pytest.raises(SystemExit):
            retranslator._cli_main()


# ===========================================================================
# (6) Sözlük ve bellek
# ===========================================================================
class TestSozlukVeBellek:
    def _book_translator(self, monkeypatch, **config):
        from core.translator import BookTranslator, TranslationConfig

        monkeypatch.setattr(BookTranslator, "_init_mt_model", lambda self: None)
        cfg = TranslationConfig(use_memory=False, run_qa=False,
                                check_consistency=False, **config)
        return BookTranslator(cfg)

    def test_core_sozlugu_yalniz_ingilizcede(self, monkeypatch):
        assert self._book_translator(monkeypatch, source_lang="en").glossary is not None
        bt = self._book_translator(monkeypatch, source_lang="de")
        assert bt.glossary is None
        assert bt.stats["glossary_terms_loaded"] == 0

    def test_baslik_sozlugu_yalniz_ingilizcede(self, monkeypatch):
        from latex_pipeline.config import PipelineConfig
        from latex_pipeline.translator import LatexTranslator

        calls = []

        def mt(self, text, is_heading=False):
            calls.append(text)
            return "Giriş Bölümü"

        monkeypatch.setattr(LatexTranslator, "_mt_call_with_timeout", mt)
        en = LatexTranslator(PipelineConfig(), source_lang="en")
        assert en._translate_heading_text("Introduction") == "Giriş"
        assert calls == []                      # İngilizcede sözlük yanıtı (aynen)
        fr = LatexTranslator(PipelineConfig(), source_lang="fr")
        assert fr._translate_heading_text("Introduction") == "Giriş Bölümü"
        assert calls == ["Introduction"]        # Fransızcada MT
        assert fr._apply_heading_glossary_wb("Section Energy") == "Section Energy"

    def test_tablo_hucresi_sozlugu_yalniz_ingilizcede(self, monkeypatch):
        from latex_pipeline.config import PipelineConfig
        from latex_pipeline.translator import LatexTranslator

        monkeypatch.setattr(LatexTranslator, "_safe_translate",
                            lambda self, t: "MT:" + t)
        de = LatexTranslator(PipelineConfig(), source_lang="de")
        assert de._translate_cell(" Material und Methode ") == " MT:Material und Methode "
        en = LatexTranslator(PipelineConfig(), source_lang="en")
        assert en._translate_cell("Introduction") == "Giriş"

    def test_yedek_sozluk_yalniz_ingilizcede(self):
        from latex_pipeline.config import PipelineConfig
        from latex_pipeline.translator import LatexTranslator

        term, tr = next(iter(LatexTranslator._GLOSSARY.items()))
        text = f"Bu cümlede {term} geçer."
        for lang, changed in (("en", True), ("de", False)):
            lt = LatexTranslator(PipelineConfig(), source_lang=lang)
            lt._glossary_engine = None           # yedek geçişin koştuğu durum
            assert (lt._apply_glossary(text) != text) is changed, lang

    def test_bellek_havuzu_kaynak_dile_gore(self, tmp_path):
        from core.mt_engines import registry
        from core.translator import BookTranslator, memory_pool_project

        assert memory_pool_project("kitap", "en") == "kitap"
        assert memory_pool_project("kitap", None) == "kitap"
        assert memory_pool_project("kitap", "de") == "kitap#de"
        pools = {}
        for lang in ("en", "de"):
            bt = BookTranslator.__new__(BookTranslator)
            bt.stats = {}
            bt.config = types.SimpleNamespace(
                memory_db_path=str(tmp_path / "bellek.db"), project_name="kitap",
                mt_engine=registry.DEFAULT_ENGINE, source_lang=lang)
            bt._init_memory()
            pools[lang] = bt.memory
        assert pools["en"].project_name == "kitap"          # İngilizce anahtar AYNEN
        assert pools["de"].project_name == "kitap#de"
        pools["en"].add("Die Kraft", "İngilizce varsayımıyla üretilmiş kötü çeviri")
        assert pools["de"].get("Die Kraft") is None
        assert pools["en"].get("Die Kraft") is not None
        for mem in pools.values():
            mem.close()


# ===========================================================================
# (7) Uçtan uca: sahte Ollama ile translate_book zinciri (de, ru, zh)
# ===========================================================================
class TestUctanUca:
    @pytest.mark.parametrize("body,lang,name", [(DE_BODY, "de", "Almanca"),
                                                (RU_BODY, "ru", "Rusça")])
    def test_kaynak_dil_zincirde(self, fake_llm_server, monkeypatch, pipeline_env,
                                 body, lang, name):
        srv = fake_llm_server("ollama", models=[OLLAMA_MODEL],
                              reply=fake_turkish_with_echo)
        _point_sources(monkeypatch, ollama_url=srv.url)
        from latex_pipeline import translator as lt_mod

        built = []
        real_init = lt_mod.LatexTranslator.__init__

        def spy_init(self, *a, **k):
            real_init(self, *a, **k)
            built.append(self)

        monkeypatch.setattr(lt_mod.LatexTranslator, "__init__", spy_init)
        out = make_book(pipeline_env, body, f"uctan_{lang}")
        code = run_book(out, f"ollama:{OLLAMA_MODEL}", skip_qc=False)
        assert code in (0, 3), code                 # 3 = QC eşiği (PDF yine üretilir)
        log = _log(out)
        # Dil kapısı geçti, dil zincire aktı.
        assert f"Kaynak dil: {name}" in log
        assert f"Phase 5: MT Translation ({lang.upper()} -> TR" in log
        assert built and {t.source_lang for t in built} == {lang}
        assert {t._translator.config.source_lang for t in built
                if t._translator is not None} == {lang}
        # İstemde doğru kaynak dil adı; hedef Türkçe.
        texts = _chat_texts(srv)
        src_name = {"de": "German", "ru": "Russian"}[lang]
        assert texts and all(f"from {src_name} to Turkish" in t for t in texts)
        # Yer tutucular korundu, model ham LaTeX görmedi.
        translated = _translated(out)
        for token in ("\\label{ch:planet}", "\\cite{kepler1609}",
                      "\\ref{ch:planet}", "$v^2 = GM(2/r - 1/a)$", "\\footnote{",
                      "\\begin{equation}", "\\label{eq:energie}"):
            assert token in translated, token
        for t in texts:
            assert "\\cite" not in t and "$v^2" not in t
        # Çevrilmemiş satır tespiti kaynak dilde çalıştı (Phase 5.7) ve
        # sözlük geçişi (EN->TR) atlandı.
        assert f"Retranslator: Untranslated ({lang}) detected" in log
        assert "Phase 5.75: sözlük geçişi atlandı" in log

    def test_ingilizce_zincirde_sozluk_gecisi_kosar(self, fake_llm_server, monkeypatch,
                                                    pipeline_env):
        srv = fake_llm_server("ollama", models=[OLLAMA_MODEL], reply=fake_turkish)
        _point_sources(monkeypatch, ollama_url=srv.url)
        out = make_book(pipeline_env, EN_BODY, "uctan_en")
        assert run_book(out, f"ollama:{OLLAMA_MODEL}", skip_qc=False) in (0, 3)
        log = _log(out)
        assert "Phase 5: MT Translation (EN -> TR" in log
        assert "Phase 5.75: Post-edit glossary pass complete" in log
        assert "sözlük geçişi atlandı" not in log

    def test_cince_belge_cikis_4(self, fake_llm_server, monkeypatch, pipeline_env):
        srv = fake_llm_server("ollama", models=[OLLAMA_MODEL], reply=fake_turkish)
        _point_sources(monkeypatch, ollama_url=srv.url)
        out = make_book(pipeline_env, ZH_BODY, "uctan_zh")
        assert run_book(out, f"ollama:{OLLAMA_MODEL}") == tb.EXIT_SOURCE_UNSUPPORTED
        assert srv.requests_to("/api/chat") == []
        assert not (out / "source" / "translated_body.tex").exists()
        assert "Bu kitabın dili desteklenmiyor (tespit: Çince/Japonca/Korece)" in _log(out)


# ===========================================================================
# (8) Metinler
# ===========================================================================
class TestMetinler:
    def test_kart_aciklamasi_listeden(self):
        from core.lang_detect import TEX_SOURCE_LANGS, language_list_tr
        from ui import branding

        assert language_list_tr(TEX_SOURCE_LANGS) in branding.MODEL_DESCRIPTION
        assert "Yalnızca İngilizce" not in branding.MODEL_DESCRIPTION
        assert "Türkçeye" in branding.MODEL_DESCRIPTION

    def test_hedef_dil_secenegi_yok(self):
        src = (PROJECT_ROOT / "translate_book.py").read_text(encoding="utf-8")
        assert "--hedef-dil" not in src and "--target-lang" not in src
