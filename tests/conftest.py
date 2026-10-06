"""
Paylasilan test fixture'lari.

Tum test dosyalari bu fixture'lari otomatik olarak kullanabilir.
pytest, conftest.py'yi otomatik kesfeder - import gerekmez.
"""

import pytest
import sys
import os
import re
from pathlib import Path
from unittest.mock import MagicMock

# Proje kok dizinini path'e ekle
sys.path.insert(0, str(Path(__file__).parent.parent))

# Testler geliştirici kipinde koşar (2026-10-05, K5; TASARIM F1). Kabukta
# KITAPCEVIR_VERI_KOKU / KITAPCEVIR_YONETICI_URL tanımlıysa (ör. paketli
# kipi elle denerken) PROJECT_ROOT'u tmp_path'e yamalayan testler bile
# çıktıyı o gerçek veri köküne yazardı. Proje modülleri içe aktarılmadan
# ÖNCE silinir: registry.MODELS_DIR gibi adlar içe aktarma anında çözülür.
# Paketli kipi sınayan testler değişkeni monkeypatch.setenv ile kurar.
from config.paths import VERI_KOKU_DEGISKENI, YONETICI_URL_DEGISKENI  # noqa: E402

for _degisken in (VERI_KOKU_DEGISKENI, YONETICI_URL_DEGISKENI):
    os.environ.pop(_degisken, None)


# ---------------------------------------------------------------------------
# Mock batch translation fonksiyonu
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_batch_fn():
    """Placeholder-koruyucu mock batch translator.

    Metin parcalarini [TR]...[/TR] ile sarar, placeholder'lara dokunmaz.
    Boylece testler ceviri YAPILDI MI / placeholder KORUNDU MU kontrol edebilir.
    """
    def _batch_fn(texts, max_length=512):
        results = []
        for t in texts:
            # Placeholder'lari koru, geri kalan metni [TR] ile isle
            parts = re.split(r'(<<(?:LATEX|CMD|TERM)_\d+>>)', t)
            translated = []
            for p in parts:
                if re.fullmatch(r'<<(?:LATEX|CMD|TERM)_\d+>>', p):
                    translated.append(p)
                else:
                    translated.append(f"[TR]{p}[/TR]")
            results.append("".join(translated))
        return results
    return _batch_fn


@pytest.fixture
def mock_single_fn():
    """Placeholder-koruyucu mock tekli translator."""
    def _single_fn(text):
        parts = re.split(r'(<<(?:LATEX|CMD|TERM)_\d+>>)', text)
        translated = []
        for p in parts:
            if re.fullmatch(r'<<(?:LATEX|CMD|TERM)_\d+>>', p):
                translated.append(p)
            else:
                translated.append(f"[TR]{p}[/TR]")
        return "".join(translated)
    return _single_fn


# ---------------------------------------------------------------------------
# LatexTranslator factory (mock MT backend ile)
# ---------------------------------------------------------------------------

@pytest.fixture
def make_latex_translator(mock_batch_fn, mock_single_fn):
    """LatexTranslator factory - MT backend mock'lanmis.

    Kullanim:
        lt = make_latex_translator()               # default mock
        lt = make_latex_translator(batch_fn=...)    # ozel batch fonksiyonu
    """
    def _make(batch_fn=None, single_fn=None):
        from latex_pipeline.translator import LatexTranslator, TranslationStats
        from latex_pipeline.config import PipelineConfig

        config = PipelineConfig(
            translate=True,
            segment_timeout=60,
        )
        lt = LatexTranslator(
            pipeline_config=config,
            mt_engine="hymt-7b",
            source_lang="en",
            target_lang="tr",
        )

        # Mock BookTranslator olustur
        mock_bt = MagicMock()

        if batch_fn is None:
            mock_bt._translate_batch_hymt = MagicMock(
                side_effect=mock_batch_fn
            )
            mock_bt.translate_batch_protected = MagicMock(
                side_effect=mock_batch_fn
            )
        else:
            mock_bt._translate_batch_hymt = MagicMock(side_effect=batch_fn)
            mock_bt.translate_batch_protected = MagicMock(side_effect=batch_fn)

        if single_fn is None:
            mock_bt.translate_text = MagicMock(side_effect=mock_single_fn)
        else:
            mock_bt.translate_text = MagicMock(side_effect=single_fn)

        # Mock translator'i enjekte et
        lt._translator = mock_bt

        # translate() normalde bu alanlari set eder, biz elle set ediyoruz
        lt._checkpoint_dir = None
        lt._checkpoint_counter = 0
        lt._progress_callback = None
        lt.stats = TranslationStats()

        return lt
    return _make


# ---------------------------------------------------------------------------
# BookTranslator factory (tamamen mock'lanmis - MT modeli yuklemez)
# ---------------------------------------------------------------------------

@pytest.fixture
def make_book_translator():
    """BookTranslator factory - MT modeli yuklemeden mock olusturur.

    Not: BookTranslator.__init__ normalde MT modelini yukler.
    Bu fixture MagicMock kullanir, gercek BookTranslator'i YUKLEMEZ.
    """
    def _make(**config_overrides):
        from core.translator import TranslationConfig
        mock_bt = MagicMock()
        mock_bt.config = TranslationConfig(**config_overrides)
        mock_bt.memory = MagicMock()
        mock_bt.glossary = MagicMock()
        mock_bt.stats = {
            "segments_translated": 0,
            "segments_from_memory": 0,
            "terms_enforced": 0,
        }
        mock_bt.is_initialized = True
        return mock_bt
    return _make


# ---------------------------------------------------------------------------
# GERCEK BookTranslator (translate_text metodu gercek, MT fonksiyonlari mock)
# ---------------------------------------------------------------------------

@pytest.fixture
def real_book_translator_with_mocked_mt():
    """BookTranslator.translate_text GERCEK metodu icin test doubles.

    Strateji:
    - BookTranslator'i MagicMock(spec=BookTranslator) ile olustur
    - Gercek metodlari (translate_text, _protect_*, _restore_*, _REF_RE) bind et
    - Sadece MT katmanini (_translate_with_mt, _translate_single) mock'la

    Sahte test fixlemek icin: translate_text GERCEK koddan calissin ki
    [MT_FAIL] guard'i, memory guard'i gibi mantiklar GERCEKTEN test edilsin.
    """
    def _make(mt_return="Termodinamik ilkeleri önemlidir.", mt_side_effect=None,
              memory_enabled=True, glossary=None, **config_overrides):
        from core.translator import BookTranslator, TranslationConfig

        bt = MagicMock(spec=BookTranslator)
        bt.config = TranslationConfig(**config_overrides)
        bt.memory = MagicMock() if memory_enabled else None
        if bt.memory is not None:
            # get_or_similar default: cache miss
            bt.memory.get_or_similar = MagicMock(return_value=(None, None))
        bt.glossary = glossary
        bt.qa_checker = None
        bt.consistency_analyzer = None
        bt.ner_protector = None
        bt.naturalizer = None
        bt.ai_client = None
        bt.stats = {
            "segments_translated": 0,
            "segments_from_memory": 0,
            "terms_enforced": 0,
            "ai_reviews": 0,
        }
        bt._is_non_translatable = MagicMock(return_value=False)

        # Gercek _REF_RE
        bt._REF_RE = BookTranslator._REF_RE

        # Gercek protect/restore metodlari
        bt._protect_references = lambda text: BookTranslator._protect_references(bt, text)
        bt._restore_references = lambda text, refs: BookTranslator._restore_references(bt, text, refs)
        bt._protect_formulas = MagicMock(return_value=("", []))
        bt._restore_formulas = lambda t, f: t
        bt._protect_units = MagicMock(return_value=("", []))
        bt._restore_units = lambda t, u: t
        # _protect_page_refs (2026-07-06, sayfa-ayracı stash) da translate_text
        # zincirinde — gerçek metodu bind et ki zincir kopmasın.
        bt._protect_page_refs = lambda text: BookTranslator._protect_page_refs(bt, text)
        bt._restore_page_refs = lambda t, p: BookTranslator._restore_page_refs(bt, t, p)

        def _passthrough(text):
            return text, []
        bt._protect_formulas.side_effect = _passthrough
        bt._protect_units.side_effect = _passthrough

        # MT mock
        if mt_side_effect is not None:
            bt._translate_with_mt = MagicMock(side_effect=mt_side_effect)
        else:
            bt._translate_with_mt = MagicMock(return_value=mt_return)

        bt._translate_single = MagicMock(return_value=mt_return)

        return bt
    return _make


@pytest.fixture
def real_latex_translator_with_mocked_bt(make_latex_translator):
    """Gercek LatexTranslator, BookTranslator mock'lu.

    make_latex_translator zaten bunu yapiyor — alias olarak yeniden export.
    """
    return make_latex_translator


@pytest.fixture
def mock_hymt_oom():
    """HY-MT 7B OOM (CUDA out of memory) exception firlatan mock."""
    def _oom(*args, **kwargs):
        raise RuntimeError("CUDA out of memory. Tried to allocate 2.5 GiB.")
    return _oom


@pytest.fixture
def sample_full_book_body():
    """Gercekci 500+ paragrafli sentetik kitap body.

    TOC + preface + chapter + formula + tabular + bibliography iceriyor.
    Full-book stress test icin.
    """
    parts = []
    # TOC (on kisim - ilk 10 sayfa)
    parts.append(r"\tableofcontents")
    parts.append("Preface\n\nThis book covers fundamental concepts in physics and engineering.")
    for i in range(10):
        parts.append(f"Chapter {i+1} .... {i*10 + 5}")

    # Ana govde (50 chapter × 10 paragraf = 500 paragraf)
    for ch in range(50):
        parts.append(rf"\chapter{{Chapter {ch+1}: Topic {ch}}}")
        for p in range(10):
            if p % 5 == 0:
                # Formul iceren paragraf
                parts.append(
                    f"In section {ch}.{p}, we derive the equation "
                    rf"$E_{{{ch}}} = mc^2 \cdot \alpha_{{{p}}}$ which describes "
                    f"the energy transfer. See Fig. \\ref{{fig:ch{ch}p{p}}}."
                )
            elif p % 7 == 0:
                # Tabular
                parts.append(
                    f"Table {ch}.{p} shows the parameters:\n"
                    r"\begin{tabular}{lll}" "\n"
                    f"Param {ch} & Value {p} & Unit \\\\\n"
                    r"\end{tabular}"
                )
            else:
                # Normal paragraf
                parts.append(
                    f"Paragraph {ch}.{p}: The analysis of this phenomenon "
                    f"requires careful consideration of {ch*p} factors. "
                    f"Smith et al. \\cite{{smith{ch}_{p}}} showed that "
                    f"this is fundamentally important for understanding the topic."
                )

    # Son kisim (index + bibliography - son 10 sayfa)
    parts.append(r"\bibliography{refs}")
    parts.append("Index\n\nEnergy, 42\nMomentum, 108\nThermodynamics, 205")
    for i in range(10):
        parts.append(f"[{i+1}] Author{i}, Title{i}, Year 20{i:02d}.")

    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# Yerel model katmanı (2026-09-25, K0): yalıtım, sahte sunucular, sahte llama
# ---------------------------------------------------------------------------
# Sözleşme: core/mt_engines/contracts.py modül docstring'i ("Test yalıtım
# sözleşmesi"). Hiçbir test gerçek Ollama (11434) / LM Studio (1234)
# sunucusuna, gerçek models/ klasörüne, gerçek config/mt_*.json dosyalarına
# ya da kurulu llama-cpp-python'a dokunmaz. Gerçek model testleri
# @pytest.mark.yerel_model ile işaretlenir, varsayılan koşuda seçilmez
# (pytest.ini) ve yalıtım onlara uygulanmaz.

import shutil  # noqa: E402
import tempfile  # noqa: E402

from tests.fakes import (  # noqa: E402
    FakeLLMServer,
    apply_mt_isolation,
    closed_local_port,
    make_fake_llama_module,
)


@pytest.fixture(scope="session")
def _mt_isolation_session(tmp_path_factory):
    """Oturum boyu: yalıtım klasörlerinin kökü + kapalı yerel port."""
    return {"base": tmp_path_factory.mktemp("mt_yalitim"),
            "closed_port": closed_local_port()}


@pytest.fixture(autouse=True)
def _mt_isolation(request, _mt_isolation_session):
    """Her testte yerel model katmanını yalıt (tests/fakes.apply_mt_isolation).

    Ayrı bir MonkeyPatch kullanır: testin kendi ``monkeypatch.undo()``
    çağrısı yalıtımı kaldırmaz. Dönüş: ``tests.fakes.MtIsolation`` (yerel
    model testlerinde None).
    """
    node = request.node
    if node.get_closest_marker("yerel_model") or node.get_closest_marker("integration"):
        yield None
        return
    root = Path(tempfile.mkdtemp(prefix="t", dir=str(_mt_isolation_session["base"])))
    mp = pytest.MonkeyPatch()
    try:
        yield apply_mt_isolation(mp, root, _mt_isolation_session["closed_port"])
    finally:
        mp.undo()
        shutil.rmtree(root, ignore_errors=True)


# ---------------------------------------------------------------------------
# Donanım kapısı (2026-09-28, KARARLAR 17): testler makineden bağımsız
# ---------------------------------------------------------------------------
# core.hardware'ın ölçümü (gerçekte MLX mx.device_info() / sysctl) her testte
# BÜYÜK sabit bir değere yamalanır: genel kapı açık, her sahte model toplu iş
# DEFAULT_BATCH_SIZE_HYMT_LOCAL ile sığar, yani donanım kapısı mevcut
# davranışı değiştirmez. 8 / 16 / 36 GB senaryoları kendi testinde yamalanır
# (tests/test_donanim_20260928.py). Değerler sahte test verisidir: 128 GB'lık
# bir Mac'in çalışma kümesi (%75 = 96 GB) düzeninde.
TEST_HARDWARE_MEMORY_BYTES = 128 * 2 ** 30
TEST_HARDWARE_AVAILABLE_BYTES = 96 * 2 ** 30
TEST_HARDWARE_CHIP = "Test Mac"


@pytest.fixture(autouse=True)
def _hardware_isolation(request):
    """Her testte donanım ölçümünü sabitle (``core.hardware._measure`` + önbellek).

    Ayrı bir MonkeyPatch kullanır (testin ``monkeypatch.undo()``'su yalıtımı
    kaldırmaz). Dönüş: kullanılan ``HardwareInfo`` (yerel model testlerinde
    None: onlar gerçek donanımı ölçer).
    """
    node = request.node
    if node.get_closest_marker("yerel_model") or node.get_closest_marker("integration"):
        yield None
        return
    from core import hardware

    info = hardware.HardwareInfo(available_bytes=TEST_HARDWARE_AVAILABLE_BYTES,
                                 memory_bytes=TEST_HARDWARE_MEMORY_BYTES,
                                 chip=TEST_HARDWARE_CHIP, source=hardware.SOURCE_METAL)
    mp = pytest.MonkeyPatch()
    mp.setattr(hardware, "_measure", lambda: info)
    mp.setattr(hardware, "_CACHE", None)
    try:
        yield info
    finally:
        mp.undo()


@pytest.fixture
def mt_isolation(_mt_isolation):
    """Yalıtım değerleri (models_dir, closed_url, sources, ...) — okumak için."""
    return _mt_isolation


@pytest.fixture
def fake_llm_server():
    """Fabrika: ``fake_llm_server("ollama"|"openai"|"lmstudio", models=..., ...)``.

    Başlatılmış ``FakeLLMServer`` döner; test sonunda hepsi durdurulur.
    """
    created = []

    def _make(flavor="ollama", *args, **kwargs):
        srv = FakeLLMServer(flavor, *args, **kwargs)
        created.append(srv)
        return srv

    yield _make
    for srv in created:
        srv.stop()


@pytest.fixture
def fake_ollama(fake_llm_server):
    """Çalışan sahte Ollama (varsayılan model: "qwen3:8b"); ``.url`` kök adres."""
    return fake_llm_server("ollama")


@pytest.fixture
def fake_openai(fake_llm_server):
    """Çalışan sahte OpenAI uyumlu sunucu (model: "qwen"); ``.base_url`` = kök + /v1."""
    return fake_llm_server("openai")


@pytest.fixture
def fake_lmstudio(fake_llm_server):
    """Çalışan sahte LM Studio (model: "qwen/qwen3-8b"); ``.base_url`` = kök + /v1."""
    return fake_llm_server("lmstudio")


@pytest.fixture
def fake_llama(monkeypatch, _mt_isolation):
    """``sys.modules["llama_cpp"]`` = sahte modül (``mod.config`` ile ayarlanır)."""
    mod = make_fake_llama_module()
    monkeypatch.setitem(sys.modules, "llama_cpp", mod)
    return mod
