"""G2b-2 (2026-10-05): düşünerek çeviri kipi — Berke KARAR 22 (KARARLAR 16'yı
kısmen değiştirir): "düşünme kapatılamıyorsa düşünecek, o şekilde çevirecek;
ona göre mimari".

Kapsam (madde -> sınıf):

- (i)   kapatılamayan model pay içinde düşünüp cevap verir -> çeviri biter,
        çıkış 0, düşünce çıktıda yok                     -> TestDusunerekCeviriUctanUca
- (ii)  düşünmesini hiç bitirmeyen parça -> TEK yeniden deneme -> özgün dilde
        + işaretli, koşu sürer                           -> TestCevapsizParca
- (iii) parçaların çoğunda cevap yok -> kesici koşu ORTASINDA açılır (ilk 5
        parçadan sonra) -> çıkış 5, saniyeler içinde     -> TestDevreKesiciUctanUca
- (iv)  "off" destekli model -> LM Studio yerel REST kapatma yolu, düşünme
        görülmez                                         -> TestKapatilabilirModel
- (v)   kapatma istenip düşünce gelince yapışkan bayrak -> düşünerek çeviri
        kipine geçiş                                     -> TestKipGecisi
- pay, istek sınırı, zaman aşımı, bellek (KV), parça başı günlük, kayan
  pencere (RAPOR MT-13), istisnanın yutulmaması (h), MLX yolu -> birim sınıfları

Gerçek Ollama / LM Studio'ya gidilmez, model yüklenmez: sahte sunucu
(tests/fakes/fake_llm_server.py; LM Studio yerel REST rotası ``native_chat``)
ve sahte ``mlx_lm`` (tests/test_token_dusunme_20260928.py yardımcıları, test_vtd
ile aynı biçimde içe aktarılır). Girdiler sentetiktir (KARAR 26: kitap metni yok).
"""

from __future__ import annotations

import logging
import math
import re
import time

import pytest

from config import defaults as D
from core import hardware
from core.mt_engines import backends as B
from core.mt_engines import contracts as C
from core.mt_engines import discovery, registry
from core.mt_engines.base import EngineSpec
from core.mt_engines.chat import ChatTemplateEngine
from core.mt_engines.contracts import (
    BackendUnavailable,
    GenerationError,
    GenerationRequest,
    GenerationResult,
    SourcesConfig,
    ThinkingBudgetExceeded,
)
from core.translator import BookTranslator, TranslationConfig
from tests import test_token_dusunme_20260928 as TT
from tests.fakes import FakeModel, FakeReply, write_fake_mlx_dir
from tests.fakes.fake_llm_server import extract_chat_text
from ui import branding

INSTR = "Translate from English to Turkish. Output only the translation."
TEXT = "The weather is nice today."
ANSWER = "Bugün hava güzel."
LMS_MODEL = "org/dusunur"
ENGINE_ID = f"lmstudio:{LMS_MODEL}"
# Eski 512'lik bütçenin ÜSTÜNDE, pay tabanının ALTINDA düşünen model
# (TestSabitler.test_test_sayilari_sabitlerle_uyumlu sınar).
N_THINK = 768
# Pay tavanını aşan, hiç bitmeyen düşünme (aynı test sınar).
ENDLESS_WORDS = 6000
# Başarılı parçaların kısa düşünmesi
SHORT_THINK_WORDS = 40
# LM Studio yerel REST "reasoning" kapatma değeri (belgeli API değişmezi)
C_REASONING_OFF = "off"
BACKENDS_LOG = "core.mt_engines.backends"
_OFF = dict(use_memory=False, glossary_mode="off", run_qa=False, check_consistency=False)
_NUMBER_WORDS = ("one", "two", "three", "four", "five", "six", "seven", "eight", "nine",
                 "ten", "eleven", "twelve", "thirteen", "fourteen", "fifteen", "sixteen",
                 "seventeen", "eighteen", "nineteen", "twenty")


# ---------------------------------------------------------------------------
# Yardımcılar
# ---------------------------------------------------------------------------
def _thought(words: int) -> str:
    return " ".join(["düşünce"] * words)


_WORDS = {
    "paragraph": "paragraf", "describes": "anlatır", "the": "bu", "long": "uzun",
    "journey": "yolculuk", "of": "ait", "outer": "dış", "planet": "gezegen",
    "around": "çevresinde", "distant": "uzak", "sun": "güneş", "in": "içinde",
    "detail": "ayrıntı", "motion": "hareket", "weather": "hava", "is": "dır",
    "nice": "güzel", "today": "bugün",
}
_PLACEHOLDER_SPLIT_RE = re.compile(r"(<<[A-Z]+_\d+>>)")
_WORD_RE = re.compile(r"[A-Za-z]+")


def _fake_turkish(text: str) -> str:
    """Sözcük sözcük sahte Türkçe (yankı sezgisine takılmaz); yer tutucular AYNEN."""
    def word(m: re.Match) -> str:
        tr = _WORDS.get(m.group(0).lower(), "sözcük")
        return tr.capitalize() if m.group(0)[0].isupper() else tr

    return "".join(part if _PLACEHOLDER_SPLIT_RE.fullmatch(part) else _WORD_RE.sub(word, part)
                   for part in _PLACEHOLDER_SPLIT_RE.split(text))


def _paragraph(word: str) -> str:
    # ~95 karakter: MT_THINK_BREAKER_SHORT_CHARS'tan uzun (gövde penceresi)
    return (f"Paragraph {word} describes the long journey of the outer planet around "
            f"the distant sun in detail.")


def _body(count: int) -> str:
    return ("\\chapter{Motion}\n\n"
            + "\n\n".join(_paragraph(w) for w in _NUMBER_WORDS[:count]) + "\n")


def _paragraph_word(text: str):
    m = re.search(r"Paragraph (\w+)\b", text or "")
    return m.group(1) if m else None


def _lms_spec(base_url, model=LMS_MODEL, **kw):
    kw.setdefault("format", "chat")
    return EngineSpec(name=f"lmstudio:{model}", backend="openai", model=model,
                      base_url=base_url, flavor="lmstudio", source="adhoc", **kw)


def _loaded(spec):
    backend = B.create_backend(spec)
    backend.load()
    return backend, ChatTemplateEngine(spec)


def _req(text=TEXT, max_tokens=D.MT_OUTPUT_TOKENS_FLOOR):
    return GenerationRequest(INSTR, text, max_tokens)


def _gen_requests(srv):
    """Üretim istekleri (ısınma / uç yoklaması — 1 token'lık istek — hariç)."""
    out = []
    for rec in srv.request_details:
        body = rec.json if isinstance(rec.json, dict) else {}
        if rec.path not in ("/v1/chat/completions", "/api/v1/chat"):
            continue
        if body.get("max_tokens") == 1 or body.get("max_output_tokens") == 1:
            continue
        out.append(rec)
    return out


def _req_text(rec) -> str:
    body = rec.json if isinstance(rec.json, dict) else {}
    if rec.path == "/api/v1/chat":
        return str(body.get("input") or "")
    return extract_chat_text(body.get("messages") or [])


def _lms_server(fake_llm_server, reply, *, options=None, native=False):
    """Sahte LM Studio; *options*: REST allowed_options (None -> ["off", "on"])."""
    model = FakeModel(LMS_MODEL, thinking=True, loaded=True, reasoning_options=options)
    return fake_llm_server("lmstudio", models=[model], reply=reply, native_chat=native)


def _point_lmstudio(monkeypatch, srv) -> None:
    sources = SourcesConfig(ollama_url="http://127.0.0.1:9", lmstudio_url=srv.base_url)
    monkeypatch.setattr(discovery, "load_sources", lambda path=None: sources)


def _make_lt(engine_id=ENGINE_ID):
    from latex_pipeline.config import PipelineConfig
    from latex_pipeline.translator import LatexTranslator

    return LatexTranslator(pipeline_config=PipelineConfig(translate=True, segment_timeout=30),
                           mt_engine=engine_id, source_lang="en", target_lang="tr",
                           translation_overrides=dict(_OFF))


def _recommended_label() -> str:
    return discovery.engine_label(registry.resolve_spec(registry.DEFAULT_ENGINE))


@pytest.fixture
def make_bt(monkeypatch):
    def _make(spec, **cfg):
        TT._patch_engine(monkeypatch, spec)
        return BookTranslator(TranslationConfig(mt_engine=spec.name, **{**_OFF, **cfg}))
    return _make


@pytest.fixture
def restore_root_logger():
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
    import translate_book as tb

    monkeypatch.setattr(tb, "_preflight_check", lambda *a, **k: [])
    memory_db = tmp_path / "bellek.db"
    real_init_memory = BookTranslator._init_memory

    def _tmp_memory(self):
        self.config.memory_db_path = str(memory_db)
        real_init_memory(self)

    monkeypatch.setattr(BookTranslator, "_init_memory", _tmp_memory)
    return tmp_path


def _book(root, body: str):
    out = root / "kitap"
    (out / "source").mkdir(parents=True)
    (out / "full_book.md").write_text("# Kitap\n\n" + body, encoding="utf-8")
    (out / "source" / "body.tex").write_text(body, encoding="utf-8")
    return out


def _run_book(out):
    import translate_book as tb

    return tb.run_pipeline(str(out), "Düşünen Kitap", skip_translate=False, skip_compile=True,
                           skip_qc=True, book_type="science", pdf_path=None, resume=True,
                           mt_engine=ENGINE_ID)


# ===========================================================================
# Sabitler ve formüller
# ===========================================================================
class TestSabitler:
    def test_test_sayilari_sabitlerle_uyumlu(self):
        assert D.MT_THINK_BUDGET_TOKENS < N_THINK < D.MT_THINK_ALLOWANCE_BASE_TOKENS
        assert ENDLESS_WORDS > D.MT_THINK_ALLOWANCE_CAP_TOKENS
        assert len(_paragraph("one")) > D.MT_THINK_BREAKER_SHORT_CHARS
        assert SHORT_THINK_WORDS < D.MT_THINK_ALLOWANCE_BASE_TOKENS

    def test_pay_formulu_ve_tavan(self):
        base, cap = D.MT_THINK_ALLOWANCE_BASE_TOKENS, D.MT_THINK_ALLOWANCE_CAP_TOKENS
        assert B.think_allowance(0) == base
        assert B.think_allowance(300) == base + math.ceil(300 * D.MT_THINK_ALLOWANCE_PER_CHAR)
        assert B.think_allowance(10 ** 6) == cap
        assert B.think_allowance(10 ** 6, cap=777) == 777

    def test_think_budget_secenegi_tavani_ezer(self):
        assert B.resolve_think_cap(EngineSpec(name="x")) == D.MT_THINK_ALLOWANCE_CAP_TOKENS
        spec = EngineSpec(name="y", options={"think_budget": 2000})
        assert B.resolve_think_cap(spec) == 2000
        assert B.ThinkingGuard(B.resolve_think_budget(spec), B.resolve_think_cap(spec)) \
            .allowance(10 ** 6) == 2000

    def test_dusunen_istek_varsayilan_baglama_sigar(self):
        # giriş üst sınırı + en uzun parçanın çeviri sınırı + pay tavanı <= bağlam
        need = (D.MT_MAX_INPUT_TOKENS + B.output_token_limit(D.MAX_SEGMENT_LENGTH)
                + D.MT_THINK_ALLOWANCE_CAP_TOKENS)
        assert need <= D.MT_CONTEXT_TOKENS

    def test_eski_esik_sabiti_kalkti(self):
        assert not hasattr(D, "MT_THINK_BREAKER_THRESHOLD")
        assert 0 < D.MT_THINK_BREAKER_RATIO <= 1
        assert 0 < D.MT_THINK_BREAKER_MIN_SAMPLES <= D.MT_THINK_BREAKER_WINDOW


# ===========================================================================
# Kayan pencere (RAPOR MT-13 kök nedeni) ve yeni anlam
# ===========================================================================
def _expected_trip(prefix_ok: int) -> int:
    """Kural (config/defaults.py): son WINDOW örnekte, en az MIN_SAMPLES örnek
    varken cevapsız oranı >= RATIO. *prefix_ok* başarıdan sonra kaçıncı cevapsızda açılır."""
    window = [False] * prefix_ok
    for k in range(1, 10_000):
        window.append(True)
        recent = window[-D.MT_THINK_BREAKER_WINDOW:]
        if len(recent) >= D.MT_THINK_BREAKER_MIN_SAMPLES and \
                sum(recent) >= D.MT_THINK_BREAKER_RATIO * len(recent) - 1e-9:
            return k
    raise AssertionError("kural hiç açılmıyor")


class TestKayanPencere:
    def test_ilk_5ten_sonra_da_acilir(self):
        guard = B.ThinkingGuard(D.MT_THINK_BUDGET_TOKENS)
        for _ in range(6):
            assert guard.note_segment(False) is None
        opened = None
        for k in range(1, 50):
            if guard.note_segment(True):
                opened = k
                break
        assert opened == _expected_trip(6)
        assert guard.tripped and "Bu model düşünmesini bitirip çeviri üretemiyor" in guard.tripped
        assert _recommended_label() in guard.tripped

    def test_mt13_gemma_sirasi_artik_acilir(self):
        # G2b-1 yeniden üretimi (OUT/G2b-1/mt13_yeniden_uretim.py): ilk dört kısa
        # başlık çevrildi, sonra gövde parçalarının çoğu cevapsız. Eski pencere
        # 1/5 ile kapanıyor, kesici hiç açılmıyordu.
        guard = B.ThinkingGuard(D.MT_THINK_BUDGET_TOKENS)
        for _ in range(4):
            guard.note_segment(False, short=True)
        for _ in range(311):
            guard.note_segment(True)
        assert guard.tripped

    def test_az_ornekle_acilmaz(self):
        guard = B.ThinkingGuard(D.MT_THINK_BUDGET_TOKENS)
        for _ in range(D.MT_THINK_BREAKER_MIN_SAMPLES - 1):
            assert guard.note_segment(True) is None
        assert guard.note_segment(True)                  # en az örnek sayısına varınca

    def test_kisa_parcalar_ayri_pencerede(self):
        guard = B.ThinkingGuard(D.MT_THINK_BUDGET_TOKENS)
        opened = None
        for k in range(1, 30):
            guard.note_segment(False, short=True)        # başlıklar çevriliyor
            if guard.note_segment(True, short=False):    # gövde cevapsız
                opened = k
                break
        # Birleşik pencerede oran 0,5'te kalırdı; ayrı pencerede gövde tek başına açar
        assert opened == D.MT_THINK_BREAKER_MIN_SAMPLES

    def test_basarili_kosuda_acilmaz(self):
        guard = B.ThinkingGuard(D.MT_THINK_BUDGET_TOKENS)
        for k in range(200):
            assert guard.note_segment(k % 7 == 0) is None    # seyrek cevapsız parça
        assert guard.tripped is None


# ===========================================================================
# Zaman aşımı (d) ve deneme planı
# ===========================================================================
class TestZamanAsimi:
    CHARS, OUT = 300, D.MT_OUTPUT_TOKENS_FLOOR
    N = 1 + D.MT_THINK_RETRY_COUNT

    def _expected(self, rate):
        return D.MT_THINK_TIMEOUT_BASE_S + (self.OUT + self.N * B.think_allowance(self.CHARS)) \
            / rate * D.MT_THINK_TIMEOUT_SAFETY

    def test_olcum_yokken_varsayilan_hiz_ve_tavan(self):
        guard = B.ThinkingGuard(D.MT_THINK_BUDGET_TOKENS)
        assert guard.rate() == D.MT_THINK_ASSUMED_TOKENS_PER_S
        assert guard.item_seconds(self.CHARS, self.OUT, 1e6) == pytest.approx(
            self._expected(D.MT_THINK_ASSUMED_TOKENS_PER_S))
        assert guard.item_seconds(self.CHARS, self.OUT, 60.0) == 60.0

    def test_olculen_hiz_kullanilir(self):
        guard = B.ThinkingGuard(D.MT_THINK_BUDGET_TOKENS)
        tokens = D.MT_THINK_RATE_MIN_TOKENS * 10
        guard.record_rate(GenerationResult(thinking_tokens=tokens, thinking_seconds=tokens / 100))
        assert guard.rate() == pytest.approx(100.0)
        assert guard.item_seconds(self.CHARS, self.OUT, 1e6) == pytest.approx(self._expected(100.0))

    def test_kucuk_ve_tahmini_ornek_hizi_degistirmez(self):
        guard = B.ThinkingGuard(D.MT_THINK_BUDGET_TOKENS)
        guard.record_rate(GenerationResult(thinking_tokens=D.MT_THINK_RATE_MIN_TOKENS - 1,
                                           thinking_seconds=0.01))
        guard.record_rate(GenerationResult(thinking_tokens=5000, thinking_estimated=True,
                                           thinking_seconds=0.01))
        guard.record_rate(GenerationResult(thinking_tokens=5000, thinking_seconds=None))
        assert guard.rate() == D.MT_THINK_ASSUMED_TOKENS_PER_S

    def test_deneme_plani(self):
        guard = B.ThinkingGuard(D.MT_THINK_BUDGET_TOKENS)
        item = guard.item_seconds(self.CHARS, self.OUT, 1e6)
        budget, limit = guard.plan(self.CHARS, self.OUT, True, 1e6)
        assert budget == B.think_allowance(self.CHARS)        # ölçüm yokken pay kısılmaz
        assert limit == pytest.approx(item)                   # denemeler süreyi paylaşır
        # Düşünerek çeviri dışında eski davranış: yoklama bütçesi, istek zaman aşımı
        assert guard.plan(self.CHARS, self.OUT, False, 240.0) == (D.MT_THINK_BUDGET_TOKENS,
                                                                  240.0)

    def test_tavan_baglayinca_pay_sureye_sigdirilir(self):
        slow = B.ThinkingGuard(D.MT_THINK_BUDGET_TOKENS)
        rate = 9.0                                            # qwen3.8-27b ölçüsü (~9 token/sn)
        slow.record_rate(GenerationResult(thinking_tokens=900, thinking_seconds=900 / rate))
        budget, limit = slow.plan(self.CHARS, self.OUT, True, 240.0)
        assert limit == 240.0                                 # tavan bağladı
        room = rate * (240.0 - D.MT_THINK_TIMEOUT_BASE_S) - self.OUT
        assert budget == int(room // self.N) < B.think_allowance(self.CHARS)
        fast = B.ThinkingGuard(D.MT_THINK_BUDGET_TOKENS)       # gemma ölçüsü (~65 token/sn)
        fast.record_rate(GenerationResult(thinking_tokens=6500, thinking_seconds=100.0))
        budget, limit = fast.plan(self.CHARS, self.OUT, True, 240.0)
        assert budget == B.think_allowance(self.CHARS) and limit < 240.0

    def test_istek_suresi_dusunerek_ceviride_olceklenir(self, fake_llm_server):
        srv = _lms_server(fake_llm_server, lambda t: FakeReply(content=ANSWER,
                                                               thinking=_thought(N_THINK)),
                          options=["on"])
        backend, eng = _loaded(_lms_spec(srv.base_url))
        assert backend.thinking_mode()
        assert backend.thinking_item_seconds(len(TEXT), D.MT_OUTPUT_TOKENS_FLOOR) <= \
            backend.request_timeout()
        res = backend.generate(eng, _req())
        assert res.error is None and res.thinking_seconds is not None
        # İlk ölçümden sonra hız ölçülmüş değerdir (sahte sunucu çok hızlı -> kısa süre)
        assert backend.think_guard.rate() > D.MT_THINK_ASSUMED_TOKENS_PER_S


# ===========================================================================
# Bellek (e)
# ===========================================================================
class TestBellek:
    W = D.HW_REFERENCE_WEIGHTS_BYTES

    @property
    def CAP(self):          # noqa: N802 — sabit gibi okunur
        return D.MT_THINK_ALLOWANCE_CAP_TOKENS

    def _hw(self, available):
        return hardware.HardwareInfo(available_bytes=int(available), memory_bytes=None,
                                     chip="Test", source=hardware.SOURCE_METAL)

    def test_toplu_kipte_pay_kvye_girer_toplu_is_kuculur(self):
        kv = hardware.reference_kv()
        roomy = self._hw(hardware.assess(self.W, kv, hw=self._hw(10 ** 12)).required_bytes)
        plain = hardware.assess(self.W, kv, hw=roomy)
        think = hardware.assess(self.W, kv, hw=roomy, think_tokens=self.CAP)
        assert plain.batch_size == D.DEFAULT_BATCH_SIZE_HYMT_LOCAL
        assert think.fits and think.batch_size < plain.batch_size
        assert think.kv_tokens == hardware.batched_kv_tokens(think.batch_size, self.CAP)
        assert hardware.batched_kv_tokens(1, self.CAP) == \
            hardware.batched_kv_tokens(1) + self.CAP

    def test_toplu_is_1_bile_sigmazsa_kapi(self):
        kv = hardware.reference_kv()
        big = self._hw(10 ** 12)
        plain_min = hardware.assess(self.W, kv, hw=big).minimum_bytes
        think_min = hardware.assess(self.W, kv, hw=big, think_tokens=self.CAP).minimum_bytes
        assert think_min > plain_min
        tight = self._hw((plain_min + think_min) // 2)
        assert hardware.assess(self.W, kv, hw=tight).fits is True
        fit = hardware.assess(self.W, kv, hw=tight, think_tokens=self.CAP)
        assert fit.fits is False and hardware.blocks(fit)        # mevcut kural (kapı)

    def test_baglam_kipinde_kv_degismez(self):
        kv = hardware.reference_kv()
        hw = self._hw(10 ** 12)
        a = hardware.assess(self.W, kv, mode=hardware.MODE_CONTEXT, hw=hw)
        b = hardware.assess(self.W, kv, mode=hardware.MODE_CONTEXT, hw=hw, think_tokens=self.CAP)
        assert a.kv_tokens == b.kv_tokens == D.MT_CONTEXT_TOKENS   # pay bağlamın içinde

    def test_kesifte_kapatilamaz_mlx_modelinde_pay(self, tmp_path, monkeypatch):
        monkeypatch.setattr(registry, "MODELS_DIR", tmp_path)
        write_fake_mlx_dir(tmp_path / "Dusunur-7B-MLX", "qwen2",
                           chat_template="{{ messages }}<think>")
        write_fake_mlx_dir(tmp_path / "Duz-7B-MLX", "qwen2", chat_template="{{ messages }}")
        think = discovery.local_memory_fit("mlx:Dusunur-7B-MLX")
        plain = discovery.local_memory_fit("mlx:Duz-7B-MLX")
        assert think.kv_tokens == hardware.batched_kv_tokens(think.batch_size or 1, self.CAP)
        assert plain.kv_tokens == hardware.batched_kv_tokens(plain.batch_size or 1)


# ===========================================================================
# (iv) kapatılabilen model: düşünme kapalı, kip YOK
# ===========================================================================
class TestKapatilabilirModel:
    def test_yerel_rest_kapatma_dusunme_gorulmez(self, fake_llm_server):
        def reply(text, body):
            if body.get("max_output_tokens") == 1:          # ısınma
                return FakeReply(content="x")
            if body.get("reasoning") == C_REASONING_OFF:      # sahte model "off"a uyar
                return FakeReply(content=ANSWER)
            return FakeReply(content=ANSWER, thinking=_thought(N_THINK))

        srv = _lms_server(fake_llm_server, reply, native=True)
        backend, eng = _loaded(_lms_spec(srv.base_url))
        assert backend.thinking_class() == C.THINKING_CAN_DISABLE
        for _ in range(3):
            res = backend.generate(eng, _req())
            assert res.error is None and res.text == ANSWER and res.thinking_tokens == 0
        gens = _gen_requests(srv)
        assert len(gens) == 3
        assert all(r.path == "/api/v1/chat" and r.json["reasoning"] == C_REASONING_OFF
                   for r in gens)
        assert not backend.thinking_monitor.sticky and not backend.thinking_mode()
        assert backend.think_guard.stats["thinking_segments"] == 0

    def test_dusunmeyen_modelde_istek_degismez(self, fake_llm_server):
        srv = fake_llm_server("lmstudio", models=[FakeModel("org/duz", thinking=False,
                                                            loaded=True)],
                              reply=lambda t: FakeReply(content=ANSWER))
        backend, eng = _loaded(_lms_spec(srv.base_url, "org/duz"))
        res = backend.generate(eng, _req())
        assert res.error is None and not backend.thinking_mode()
        body = _gen_requests(srv)[0].json
        assert body["max_tokens"] == D.MT_OUTPUT_TOKENS_FLOOR and body["stream"] is False


# ===========================================================================
# (v) kapatma tutmayınca düşünerek çeviri kipine geçiş
# ===========================================================================
class TestKipGecisi:
    def test_kapatma_tutmayinca_payla_yeniden_dener_ve_kipe_gecer(self, fake_llm_server):
        def reply(text, body):
            if body.get("max_output_tokens") == 1:
                return FakeReply(content="x")
            return FakeReply(content=ANSWER, thinking=_thought(N_THINK))   # "off" tutmuyor

        srv = _lms_server(fake_llm_server, reply, native=True)
        backend, eng = _loaded(_lms_spec(srv.base_url))
        assert backend.thinking_class() == C.THINKING_CAN_DISABLE and not backend.thinking_mode()
        res = backend.generate(eng, _req())
        assert res.error is None and res.text == ANSWER
        assert backend.thinking_monitor.off_failed and backend.thinking_mode()
        gens = _gen_requests(srv)
        assert len(gens) == 2
        assert gens[0].stream_closed_early          # 1. deneme 512'lik sınırda kesildi
        assert not gens[1].stream_closed_early      # 2. deneme payla bitti
        assert gens[1].json["max_output_tokens"] == \
            D.MT_OUTPUT_TOKENS_FLOOR + B.think_allowance(len(TEXT))
        res2 = backend.generate(eng, _req())        # sonraki parça doğrudan kipte
        assert res2.error is None and len(_gen_requests(srv)) == 3

    def test_yok_sinifli_sunucuda_beklenmedik_dusunce_kipe_gecirir(self, fake_llm_server):
        srv = fake_llm_server("openai", models=["genel"],
                              reply=lambda t: FakeReply(content=ANSWER,
                                                        thinking=_thought(N_THINK)))
        spec = EngineSpec(name=f"openai:{srv.base_url}|genel", backend="openai",
                          model="genel", base_url=srv.base_url, format="chat", source="adhoc")
        backend, eng = _loaded(spec)
        assert backend.thinking_class() == C.THINKING_UNKNOWN
        res = backend.generate(eng, _req())
        assert res.error is None
        assert backend.thinking_monitor.unexpected and backend.thinking_mode()


# ===========================================================================
# İstek sınırı (b), düşünce ayıklama ve parça başı günlük (c)
# ===========================================================================
class TestIstekVeGunluk:
    def test_istek_siniri_pay_arti_ceviri_siniri(self, fake_llm_server):
        srv = _lms_server(fake_llm_server, lambda t: FakeReply(content=ANSWER,
                                                               thinking=_thought(10)),
                          options=["on"])
        backend, eng = _loaded(_lms_spec(srv.base_url))
        assert backend.thinking_class() == C.THINKING_ALWAYS and backend.thinking_mode()
        res = backend.generate(eng, _req())
        assert res.error is None
        body = _gen_requests(srv)[0].json
        assert body["stream"] is True
        assert body["max_tokens"] == D.MT_OUTPUT_TOKENS_FLOOR + B.think_allowance(len(TEXT))

    def test_parca_basi_gunluk_gercek_olcum(self, fake_llm_server, caplog):
        srv = _lms_server(fake_llm_server, lambda t: FakeReply(content=ANSWER,
                                                               thinking=_thought(N_THINK)),
                          options=["on"])
        backend, eng = _loaded(_lms_spec(srv.base_url))
        caplog.set_level(logging.INFO, logger=BACKENDS_LOG)
        res = backend.generate(eng, _req())
        assert res.error is None and res.thinking_tokens == N_THINK
        assert res.think_budget == B.think_allowance(len(TEXT))
        assert res.thinking_seconds is not None and res.thinking_seconds >= 0
        lines = [r.getMessage() for r in caplog.records
                 if r.name == BACKENDS_LOG and "Düşünerek çeviri" in r.getMessage()]
        assert lines and f"düşünme {N_THINK} token" in lines[-1]
        assert f"pay {B.think_allowance(len(TEXT))} token" in lines[-1]

    def test_dusunce_ceviriye_karismaz(self, fake_llm_server, make_bt):
        srv = _lms_server(fake_llm_server,
                          lambda t: FakeReply(content=_fake_turkish(t),
                                              thinking=_thought(N_THINK)), options=["on"])
        bt = make_bt(_lms_spec(srv.base_url))
        out = bt.translate_text(TEXT)
        assert out == _fake_turkish(TEXT) and "düşünce" not in out


# ===========================================================================
# (f) pay dolup cevap yoksa: tek yeniden deneme (daha kısa resmî yol ya da aynı istek)
# ===========================================================================
class TestYenidenDeneme:
    def test_kisa_dusunme_yoluyla_yeniden_deneme(self, fake_llm_server):
        def reply(text, body):
            if body.get("max_output_tokens") == 1:          # uç yoklaması
                return FakeReply(content="x")
            if body.get("reasoning") == "low":
                return FakeReply(content=ANSWER, thinking=_thought(SHORT_THINK_WORDS))
            return FakeReply(content="", thinking=_thought(ENDLESS_WORDS))

        srv = _lms_server(fake_llm_server, reply, options=["low", "medium", "high"], native=True)
        backend, eng = _loaded(_lms_spec(srv.base_url))
        assert backend.thinking_class() == C.THINKING_ALWAYS and backend.thinking_mode()
        res = backend.generate(eng, _req())
        assert res.error is None and res.text == ANSWER
        gens = _gen_requests(srv)
        assert [r.path for r in gens] == ["/v1/chat/completions", "/api/v1/chat"]
        assert gens[1].json["reasoning"] == "low" and gens[1].json["store"] is False
        assert backend.think_guard.stats["think_retries"] == 1

    def test_kisaltan_yol_yapiskan_degil(self, fake_llm_server):
        def reply(text, body):
            if body.get("max_output_tokens") == 1:
                return FakeReply(content="x")
            if body.get("reasoning") == "low":
                return FakeReply(content=ANSWER, thinking=_thought(SHORT_THINK_WORDS))
            return FakeReply(content="", thinking=_thought(ENDLESS_WORDS))

        srv = _lms_server(fake_llm_server, reply, options=["low", "high"], native=True)
        backend, eng = _loaded(_lms_spec(srv.base_url))
        backend.generate(eng, _req())
        backend.generate(eng, _req(text="Another sentence is here."))
        paths = [r.path for r in _gen_requests(srv)]
        # her parça önce tam düşünür (OpenAI uyumlu uç), "low" yalnız yeniden denemede
        assert paths == ["/v1/chat/completions", "/api/v1/chat"] * 2
        assert not backend.think_guard.hard_mode

    def test_kapatan_sert_yol_ise_yarayinca_yapiskan(self, fake_llm_server):
        def reply(text, body):
            if TT._has_no_think(body):                      # Qwen3 "/no_think" tutar
                return FakeReply(content=ANSWER)
            return FakeReply(content=ANSWER, thinking=_thought(ENDLESS_WORDS))  # think:false tutmaz

        model = FakeModel("qwen3:8b", thinking=True, think_values=[True, False])
        srv = fake_llm_server("ollama", models=[model], reply=reply)
        backend, eng = _loaded(EngineSpec(name="ollama:qwen3:8b", backend="ollama",
                                          model="qwen3:8b", base_url=srv.url, format="chat",
                                          source="adhoc"))
        first = backend.generate(eng, _req())
        assert first.error is None and backend.thinking_mode()
        assert backend.think_guard.hard_mode                  # KARARLAR 22: kapatılabiliyorsa kapatılır
        second = backend.generate(eng, _req(text="Another sentence is here."))
        bodies = srv.requests_to("/api/chat")
        assert second.error is None and len(bodies) == 3 and TT._has_no_think(bodies[2])

    def test_kisa_yol_yoksa_ayni_istekle_bir_kez(self, fake_llm_server):
        srv = _lms_server(fake_llm_server,
                          lambda t: FakeReply(content="", thinking=_thought(ENDLESS_WORDS)),
                          options=["on"])
        backend, eng = _loaded(_lms_spec(srv.base_url))
        res = backend.generate(eng, _req())
        assert res.error and res.error.startswith("Model düşünme sınırını aştı")
        assert res.text == "" and res.thinking_exceeded
        gens = _gen_requests(srv)
        assert len(gens) == 2 and gens[0].json == gens[1].json
        assert backend.think_guard.stats["think_failed"] == 1
        assert backend.health() is None                       # tek parça kesiciyi açmaz

    def test_ayni_cevapsiz_istek_yeniden_gonderilmez(self, fake_llm_server):
        srv = _lms_server(fake_llm_server,
                          lambda t: FakeReply(content="", thinking=_thought(ENDLESS_WORDS)),
                          options=["on"])
        backend, eng = _loaded(_lms_spec(srv.base_url))
        first = backend.generate(eng, _req())
        n = len(_gen_requests(srv))
        again = backend.generate(eng, _req())               # latex kurtarmasının aynı isteği
        assert again.error == first.error and len(_gen_requests(srv)) == n
        backend.generate(eng, _req(text="Another sentence is here."))
        assert len(_gen_requests(srv)) == n + 2              # farklı metin modele gider

    def test_sicaklik_verilmisse_cevapsiz_istek_hatirlanmaz(self, fake_llm_server):
        srv = _lms_server(fake_llm_server,
                          lambda t: FakeReply(content="", thinking=_thought(ENDLESS_WORDS)),
                          options=["on"])
        backend, eng = _loaded(_lms_spec(srv.base_url, options={"temperature": 0.7}))
        backend.generate(eng, _req())
        n = len(_gen_requests(srv))
        backend.generate(eng, _req())                       # örneklemeli üretim: yine dener
        assert len(_gen_requests(srv)) == n + 2


class TestCevapsizParca:
    def test_tek_yeniden_deneme_sonra_ozgun_dilde_ve_isaretli(self, fake_llm_server,
                                                              monkeypatch, caplog):
        fail = "three"

        def reply(text, body=None):
            if _paragraph_word(text) == fail:
                return FakeReply(content="", thinking=_thought(ENDLESS_WORDS))
            return FakeReply(content=_fake_turkish(text), thinking=_thought(SHORT_THINK_WORDS))

        srv = _lms_server(fake_llm_server, reply, options=["on"])
        _point_lmstudio(monkeypatch, srv)
        caplog.set_level(logging.INFO, logger=BACKENDS_LOG)
        lt = _make_lt()
        out = lt.translate(_body(6))
        assert _paragraph(fail) in out                        # özgün dilinde (kayıpsız)
        for word in ("one", "two", "four", "five", "six"):
            assert _paragraph(word) not in out
        tries = [r for r in _gen_requests(srv) if _paragraph_word(_req_text(r)) == fail]
        assert len(tries) == 2 and all(r.stream_closed_early for r in tries)
        assert lt.backend_health() is None                    # koşu durmadı
        assert lt._translator.stats["think_failed"] == 1
        assert lt.stats.segments_failed >= 1                  # F3 kapısı görür
        warnings = [r.getMessage() for r in caplog.records
                    if r.name == BACKENDS_LOG and r.levelno == logging.WARNING
                    and "yeniden denemede de cevap gelmedi" in r.getMessage()]
        assert len(warnings) == 1 and "özgün dilinde" in warnings[0]


# ===========================================================================
# (h) kesici / kopma istisnası yutulmaz; öteki hatalarda eski davranış
# ===========================================================================
class TestYutulmama:
    def _bt(self):
        from core.sentence_splitter import SentenceSplitter

        bt = BookTranslator.__new__(BookTranslator)
        bt._backend = object()       # sunucu arka ucu yolu
        bt.splitter = SentenceSplitter()
        return bt

    @pytest.mark.parametrize("exc", [ThinkingBudgetExceeded("kesici açık"),
                                     BackendUnavailable("sunucu koptu")])
    def test_kesici_ve_kopma_yukari_cikar(self, monkeypatch, exc):
        def boom(self, text, max_length=512, is_heading=False):
            raise exc
        monkeypatch.setattr(BookTranslator, "_translate_single", boom)
        with pytest.raises(type(exc)):
            BookTranslator._translate_with_mt(self._bt(), TEXT)
        long_text = " ".join([TEXT] * 30)                     # cümle cümle yol da
        with pytest.raises(type(exc)):
            BookTranslator._translate_with_mt(self._bt(), long_text)

    @pytest.mark.parametrize("exc", [GenerationError("Model boş çıktı döndürdü."),
                                     RuntimeError("MT generate kilidi 300s icinde alinamadi")])
    def test_oge_hatasi_eskisi_gibi_mt_fail(self, monkeypatch, exc):
        def boom(self, text, max_length=512, is_heading=False):
            raise exc
        monkeypatch.setattr(BookTranslator, "_translate_single", boom)
        assert BookTranslator._translate_with_mt(self._bt(), TEXT) == "[MT_FAIL]" + TEXT

    def test_kesici_acilinca_translate_text_istisna_firlatir(self, fake_llm_server, make_bt):
        srv = _lms_server(fake_llm_server,
                          lambda t: FakeReply(content="", thinking=_thought(ENDLESS_WORDS)),
                          options=["on"])
        bt = make_bt(_lms_spec(srv.base_url))
        texts = [_paragraph(w) for w in _NUMBER_WORDS[:D.MT_THINK_BREAKER_MIN_SAMPLES]]
        with pytest.raises(ThinkingBudgetExceeded):
            bt._translate_batch_hymt(texts)
        assert "düşünmesini bitirip" in bt.backend_health()
        n = len(_gen_requests(srv))
        with pytest.raises(ThinkingBudgetExceeded):
            bt.translate_text(_paragraph("nine"))             # eskiden yankı dönüyordu
        assert len(_gen_requests(srv)) == n                   # açık devrede ağ yok


# ===========================================================================
# (i) uçtan uca: kapatılamayan model düşünerek çevirir -> çıkış 0
# ===========================================================================
@pytest.mark.sahte_sunucu
class TestDusunerekCeviriUctanUca:
    def test_kapatilamaz_model_cevirir_cikis_0(self, fake_llm_server, monkeypatch, pipeline_env):
        srv = _lms_server(fake_llm_server,
                          lambda t: FakeReply(content=_fake_turkish(t),
                                              thinking=_thought(N_THINK)), options=["on"])
        _point_lmstudio(monkeypatch, srv)
        out = _book(pipeline_env, _body(7))
        assert _run_book(out) == 0
        body = (out / "source" / "translated_body.tex").read_text(encoding="utf-8")
        assert "düşünce" not in body                          # düşünce çıktıya karışmadı
        for word in _NUMBER_WORDS[:7]:
            assert _paragraph(word) not in body
        log = (out / "pipeline.log").read_text(encoding="utf-8")
        assert "Düşünerek çeviri" in log and f"düşünme {N_THINK} token" in log
        assert all(not r.stream_closed_early for r in _gen_requests(srv))


# ===========================================================================
# (iii) uçtan uca: kesici koşu ORTASINDA açılır -> çıkış 5, saniyeler içinde
# ===========================================================================
@pytest.mark.sahte_sunucu
class TestDevreKesiciUctanUca:
    OK_COUNT = 6              # ilk 6 gövde parçası çevrilir (eski pencere kapanırdı)
    PARAGRAPHS = 20

    def test_kosu_ortasinda_acilir_cikis_5(self, fake_llm_server, monkeypatch, pipeline_env):
        ok_words = set(_NUMBER_WORDS[:self.OK_COUNT])

        def reply(text, body=None):
            word = _paragraph_word(text)
            if word is not None and word not in ok_words:
                return FakeReply(content="", thinking=_thought(ENDLESS_WORDS))
            return FakeReply(content=_fake_turkish(text), thinking=_thought(SHORT_THINK_WORDS))

        srv = _lms_server(fake_llm_server, reply, options=["on"])
        _point_lmstudio(monkeypatch, srv)
        out = _book(pipeline_env, _body(self.PARAGRAPHS))
        t0 = time.monotonic()
        code = _run_book(out)
        elapsed = time.monotonic() - t0
        assert code == 5
        log = (out / "pipeline.log").read_text(encoding="utf-8")
        assert "Bu model düşünmesini bitirip çeviri üretemiyor" in log
        assert _recommended_label() in log
        assert not (out / "source" / "translated_body.tex").exists()
        trip_at = self.OK_COUNT + _expected_trip(self.OK_COUNT)
        assert trip_at > 5                                    # ilk 5 parçadan SONRA
        sent = {_paragraph_word(_req_text(r)) for r in _gen_requests(srv)}
        late = [w for w in _NUMBER_WORDS[trip_at:self.PARAGRAPHS] if w in sent]
        assert late == []                                     # kalan parçalar ağa gitmedi
        assert _NUMBER_WORDS[trip_at - 1] in sent
        assert elapsed < 30.0


# ===========================================================================
# MLX yolu: düşünmesi kapatılamayan sohbet modeli
# ===========================================================================
class _NoSwitchTok(TT._ThinkTok):
    """Şablonu düşünen ama kapatma anahtarı (enable_thinking) olmayan sahte tokenizer."""
    chat_template = "<think>{{ messages }}"


class TestMlxDusunerek:
    def test_kapatilamaz_mlx_payla_cevirir(self, monkeypatch):
        spec = TT._mlx_chat_spec(name="Dusunur-7B-mlx")
        fake = TT._FakeMlx(stream_reply=lambda p: ("<think>\n" + _thought(N_THINK)
                                                   + "\n</think>\n\nÇEVRİ"))
        bt = TT._mlx_bt(monkeypatch, spec, _NoSwitchTok(), fake)
        assert bt._mlx_monitor().effective_class == C.THINKING_ALWAYS
        assert bt._mlx_thinking_mode()
        assert bt._translate_with_mt(TEXT) == "ÇEVRİ"
        limit = bt._scale_max_tokens([TEXT])
        assert fake.stream_calls[0]["max_tokens"] == limit + B.think_allowance(len(TEXT))
        assert len(fake.stream_calls) == 1

    def test_mlx_kesici_kayan_pencereyle_acilir(self, monkeypatch):
        spec = TT._mlx_chat_spec(name="Dusunur-7B-mlx")
        fake = TT._FakeMlx(stream_reply=lambda p: "<think>\n" + _thought(ENDLESS_WORDS))
        bt = TT._mlx_bt(monkeypatch, spec, _NoSwitchTok(), fake)
        texts = [_paragraph(w) for w in _NUMBER_WORDS[:D.MT_THINK_BREAKER_MIN_SAMPLES]]
        with pytest.raises(ThinkingBudgetExceeded):
            bt._translate_batch_hymt(texts)
        assert "düşünmesini bitirip" in bt.backend_health()
        n = len(fake.stream_calls)
        with pytest.raises(ThinkingBudgetExceeded):
            bt._translate_with_mt(texts[0])
        assert len(fake.stream_calls) == n


# ===========================================================================
# KARARLAR 22: Qwen3 "/no_think" resmî kapatma yolu ilk denemeden (G2b-1'in bıraktığı karar)
# ===========================================================================
class TestQwen3VarsayilanKapatma:
    def test_llamacpp_qwen3_ilk_denemeden_no_think(self, fake_llama, tmp_path):
        from tests.fakes import write_fake_gguf

        path = tmp_path / "Qwen3-8B-Q4_K_M.gguf"
        write_fake_gguf(path, "qwen3")
        fake_llama.config.chat_template = TT._THINK_TEMPLATE
        fake_llama.config.reply = lambda text, kwargs: (
            "Bugün hava güzel." if TT._has_no_think(kwargs["messages"])
            else "<think>\n" + _thought(ENDLESS_WORDS) + "\n</think>\nBugün hava güzel.")
        backend, eng = TT._loaded(TT._gguf_spec(path))
        assert backend.thinking_profile().off_path == C.THINKING_OFF_QWEN3_SOFT
        res = backend.generate(eng, _req())
        assert res.error is None and res.think_retries == 0 and len(fake_llama.calls) == 1
        assert TT._has_no_think(fake_llama.calls[0][1]["messages"])
        assert not backend.thinking_mode()

    def test_kayit_dusunme_istiyorsa_dokunulmaz(self, fake_llama, tmp_path):
        from tests.fakes import write_fake_gguf

        path = tmp_path / "Qwen3-8B-Q4_K_M.gguf"
        write_fake_gguf(path, "qwen3")
        fake_llama.config.chat_template = TT._THINK_TEMPLATE
        fake_llama.config.reply = lambda text, kwargs: "<think>\nkısa\n</think>\nBugün hava güzel."
        backend, eng = TT._loaded(TT._gguf_spec(path, think=None))
        backend.generate(eng, _req())
        assert not TT._has_no_think(fake_llama.calls[0][1]["messages"])


# ===========================================================================
# Arayüz metinleri (gösterim K6'nın)
# ===========================================================================
class TestMetinler:
    def test_branding_metinleri(self):
        assert "düşünerek çeviri" in branding.THINKING_MODE_NOTE
        text = branding.THINKING_NO_ANSWER_MESSAGE.format(window=10, count=6,
                                                          recommended="HY-MT1.5 7B (MLX)")
        assert text.startswith("Bu model düşünmesini bitirip çeviri üretemiyor")
        assert "HY-MT1.5 7B (MLX)" in text and "başka bir model seç" in text.lower()
        for word in ("düşünmesini", "üretemiyor", "çeviri", "seç"):   # Türkçe harfler
            assert word in text.lower()

    def test_kesici_iletisi_oom_dizgisi_tasimaz(self):
        reason = B.thinking_no_answer_reason(count=6, window=10)
        assert not C.has_oom_trigger(reason)
        assert "10" in reason and "6" in reason
