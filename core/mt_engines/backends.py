"""Model arka uçları: üretimin NEREDE yapıldığı (TASARIM B.3.4, 2026-09-25).

İki eksen var: **biçim** (prompt nasıl kurulur — ``MTEngine``: hymt / chat)
ve **arka uç** (üretim nerede — bu modül). MLX bugünkü gibi
``BookTranslator``'ın kendi yolunda kalır: ``create_backend`` MLX için
``None`` döner ve çevirmen hiçbir şeyi değiştirmeden eski yolu yürür
(TASARIM B.2). Yeni arka uçlar:

- ``ollama``   -> ``http_backends.OllamaBackend`` (Ollama yerel API'si)
- ``openai``   -> ``http_backends.OpenAIBackend`` (LM Studio, llama.cpp
  server, Jan, vLLM — OpenAI uyumlu uçlar)
- ``llamacpp`` -> ``llamacpp_backend.LlamaCppBackend`` (llama-cpp-python ile
  doğrudan ``.gguf``)

Sözleşme (``MTBackend``):

- ``load()`` modeli yükler/ısıtır; olmazsa ``BackendUnavailable`` (Türkçe).
- ``generate(engine, req)`` -> ``GenerationResult``. Tek öğenin hatası
  (zaman aşımı, bağlam aşımı, boş çıktı, düşünme sınırı) ``result.error``'dadır;
  arka uç tümden kullanılamazsa (devre açık, model yok, sunucu koptu, düşünme
  devre kesicisi) ``BackendUnavailable`` fırlar. Alt sınıflar tek denemeyi
  ``generate_once(engine, req, hard=..., deadline=...)`` ile verir;
  ``generate`` onu düşünme bekçisinden geçirir.
- ``generate_batch(engine, reqs)``: ``len(çıktı) == len(reqs)`` HER ZAMAN;
  sonuç sırası girdi sırasıdır. Varsayılan sıralı (``MT_HTTP_MAX_PARALLEL``
  = 1, KARARLAR 7); ``options.max_parallel > 1`` ise iş parçacığı havuzu.
- ``health()`` -> None (sağlıklı) ya da Türkçe neden (devre açık).
- ``describe()`` -> "Ollama · qwen3:8b · http://127.0.0.1:11434".

Çıktı sınırı ve düşünme (2026-09-28, KARARLAR 15-16):

- ``output_token_limit`` bütün yolların (MLX tekil/toplu, Ollama
  ``num_predict``, OpenAI ``max_tokens``, llama.cpp ``max_tokens``) üretim
  sınırının TEK hesabıdır (``BookTranslator._scale_max_tokens`` çağırır).
  ``near_output_limit`` sınıra dayanan çıktıyı tanır (çevirmen uyarır ve
  sayar; sessiz kesilme yok).
- ``generate`` bir şablondur: alt sınıfın ``generate_once(hard=...)``
  denemesini ``ThinkingGuard`` üzerinden koşturur. Her yanıtta düşünme
  ölçülür (``ThinkingMeter``: ayrı düşünme alanı, metin içi ``<think>``,
  harmony kanalı). Düşünen ya da düşündüğü ölçülen modelde yanıt akış
  hâlinde okunur ve düşünme sınırı aşılınca istek hemen kesilir.

Düşünerek çeviri (2026-10-05, G2b-2; KARARLAR 22 "düşünme kapatılamıyorsa
düşünecek, o şekilde çevirecek"):

- Düşünme resmî yolla kapatılabiliyorsa kapalıdır; o modelde sınır yalnız
  yoklamadır (``MT_THINK_BUDGET_TOKENS``; motor başına ``options.think_budget``).
- Kip (``MTBackend.thinking_mode``): etkin sınıf "kapatilamaz" (keşif ya da
  koşuda kapatma tutmadı — ``thinking_monitor`` yapışkan bayrağı; bayrağın
  konduğu denemenin yeniden denemesi bile kiptedir) ya da kayıt düşünmeyi
  istiyor ve model düşünüyor. Kipte her parçaya AYRI düşünme payı
  (``think_allowance``), istek sınırı = pay + çeviri sınırı
  (``stream_max_tokens``), zaman aşımı paya ve ÖLÇÜLEN hıza göre
  (``ThinkingGuard.plan``; tavan bağlarsa pay süreye sığdırılır), parça başı düşünme
  token'ı ve süresi günlüğe gerçek ölçümle yazılır. Pay dolup cevap gelmezse
  parça BİR KEZ yeniden denenir (resmî daha kısa düşünme yolu varsa onunla —
  ``shorter_thinking_available`` — yoksa aynı istekle); yine cevap yoksa
  ``MSG_THINK_BUDGET`` öğe hatası: parça özgün dilinde kalır, koşu sürer.
  Aynı istek o koşuda modele bir daha gönderilmez (açgözlü üretimde sonuç
  değişmez; latex kurtarma sırasının aynı metni yinelemesi ağa gitmez).
- Devre kesici (yeni anlam): düşünme aşımında DEĞİL, "model cevap
  üretemiyor"da — kipte koşu boyunca kayan pencerede cevapsız oranı eşiğe
  varınca (``MT_THINK_BREAKER_*``; kısa parçalar ayrı pencerede) devre
  ``ThinkingBudgetExceeded`` ile açılır, çevirmen yutmaz, çıkış 5; ileti
  ``ui.branding.THINKING_NO_ANSWER_MESSAGE``.
- Düşünmeyen modelde (HY-MT dahil) istek gövdesi ve davranış DEĞİŞMEZ.

Devre kesici: art arda ``MT_HTTP_CIRCUIT_MAX_FAILURES`` bağlantı düzeyi
başarısızlık (yeniden denemeler tükendikten sonra) devreyi açar; bellek
yetersizliği ve "model sunucuda yok" hemen açar. Sunucu yanıt verip parçaya
özgü HTTP hatası (500) döndürürse o parça öğe hatasıdır; ayrı sayaçla art
arda ``MT_HTTP_SERVER_ERROR_MAX_ITEMS`` parça böyle biterse devre açılır
(2026-09-29, F4-05). Açık devrede hiçbir çağrı ağa gitmez. Başarılı yanıt
iki sayacı da sıfırlar; yarı-açık kapanma yok (yeni koşu = yeni nesne).

Bağlam (2026-09-29, F4-03): ``context_budget`` üretim sınırını bağlamda
kalan yere kısar (istem sığmıyorsa istek gönderilmez); ``context_overflow``
kısılan sınıra dayanan çıktıyı öğe hatasına çevirir (kesik çeviri kabul
edilmez).

Testte yamalanabilir noktalar (çağrı anında modül özniteliği olarak
okunur): ``_sleep``, ``MT_HTTP_TIMEOUT_S``, ``MT_MODEL_LOAD_TIMEOUT_S``,
``ENGINE_CHECK_HTTP_TIMEOUT_S``, ``DEFAULT_RETRY_COUNT``,
``DEFAULT_RETRY_BASE_DELAY_S``, ``MT_HTTP_CIRCUIT_MAX_FAILURES``,
``MT_THINK_BUDGET_TOKENS``, ``MT_THINK_RETRY_COUNT``, ``MT_THINK_ALLOWANCE_*``,
``MT_THINK_ASSUMED_TOKENS_PER_S``, ``MT_THINK_RATE_MIN_TOKENS``,
``MT_THINK_TIMEOUT_*``, ``MT_THINK_BREAKER_*``,
``MT_HTTP_SERVER_ERROR_MAX_ITEMS``.

Bu modül hafiftir: mlx, torch, requests, httpx, openai, llama_cpp içe
aktarmaz. Paket ``__init__``'inden dışa aktarılmaz (contracts docstring'i).
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import logging
import math
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple, Type

from config.defaults import (
    MT_CONTEXT_CHARS_PER_TOKEN,
    MT_GREEDY_SEED,
    MT_GREEDY_TEMPERATURE,
    MT_HTTP_CIRCUIT_MAX_FAILURES,
    MT_HTTP_MAX_PARALLEL,
    MT_HTTP_MAX_PARALLEL_LIMIT,
    MT_HTTP_TIMEOUT_S,
    MT_OUTPUT_NEAR_LIMIT_RATIO,
    MT_OUTPUT_TOKENS_CAP,
    MT_OUTPUT_TOKENS_FACTOR,
    MT_OUTPUT_TOKENS_FLOOR,
    MT_REPEAT_PENALTY,
)
# Arka uç modülleri (http_backends, llamacpp_backend) bu adları BURADAN
# (``backends.<AD>``) çağrı anında okur: testler tek noktadan yamalar.
from config.defaults import (  # noqa: F401 — yamalanabilir yeniden dışa aktarım
    DEFAULT_RETRY_BASE_DELAY_S,
    DEFAULT_RETRY_COUNT,
    ENGINE_CHECK_HTTP_TIMEOUT_S,
    MT_MODEL_LOAD_TIMEOUT_S,
    MT_THINK_ALLOWANCE_BASE_TOKENS,
    MT_THINK_ALLOWANCE_CAP_TOKENS,
    MT_THINK_ALLOWANCE_PER_CHAR,
    MT_THINK_ASSUMED_TOKENS_PER_S,
    MT_THINK_BREAKER_MIN_SAMPLES,
    MT_THINK_BREAKER_RATIO,
    MT_THINK_BREAKER_SHORT_CHARS,
    MT_THINK_BREAKER_WINDOW,
    MT_THINK_BUDGET_TOKENS,
    MT_THINK_RATE_MIN_TOKENS,
    MT_THINK_RETRY_COUNT,
    MT_THINK_TIMEOUT_BASE_S,
    MT_THINK_TIMEOUT_SAFETY,
)

from .chat import (
    HARMONY_FINAL_RE,
    HARMONY_MARK,
    NO_THINK_SWITCH,
    THINK_CLOSE_RE,
    THINK_OPEN_RE,
    source_has_think,
)
from .contracts import (
    BACKEND_LLAMACPP,
    BACKEND_MLX,
    BACKEND_OLLAMA,
    BACKEND_OPENAI,
    MSG_BAD_ID,
    MSG_BAD_RESPONSE,
    MSG_CONTEXT_EXCEEDED,
    MSG_GENERATE_FAILED,
    MSG_LOAD_FAILED,
    MSG_SERVER_ERROR,
    MSG_THINK_BUDGET,
    MSG_TIMEOUT,
    THINKING_ALWAYS,
    THINKING_OFF_QWEN3_SOFT,
    BackendUnavailable,
    GenerationRequest,
    GenerationResult,
    ThinkingBudgetExceeded,
    oom_safe,
)
from .thinking import ThinkingOffMonitor, ThinkingProfile

logger = logging.getLogger(__name__)

__all__ = [
    "MTBackend",
    "create_backend",
    "MSG_SERVER_ERROR",
    "MSG_BAD_RESPONSE",
    "MSG_LOAD_FAILED",
    "MSG_GENERATE_FAILED",
    "estimate_tokens",
    "context_error",
    "context_budget",
    "context_overflow",
    "is_context_error",
    "MT_HTTP_SERVER_ERROR_MAX_ITEMS",
    "short_detail",
    "format_seconds",
    "output_token_limit",
    "near_output_limit",
    "inline_thinking",
    "ThinkingMeter",
    "ThinkingGuard",
    "THINK_STAT_KEYS",
    "resolve_think_budget",
    "resolve_think_cap",
    "think_allowance",
    "short_item",
    "is_timeout_error",
    "is_no_answer",
    "thinking_no_answer_reason",
    "wants_thinking_off",
    "supports_no_think",
    "with_no_think",
    "template_signals_thinking",
    "prompt_opens_thinking",
    "template_opens_thinking",
]

# Testte gerçek bekleme olmasın diye yamalanır (backends._sleep).
_sleep = time.sleep

# MSG_SERVER_ERROR, MSG_BAD_RESPONSE, MSG_LOAD_FAILED, MSG_GENERATE_FAILED
# 2026-09-28'de contracts.MESSAGE_CATALOG'a taşındı (tek katalog, metin aynı);
# bu modülden de (``backends.MSG_*``) okunmaya devam eder.

# Hata ayrıntısından iletiye alınan en çok karakter (ileti tek satır kalsın).
_DETAIL_MAX_CHARS = 300

# 2026-09-29 (F4-05): sunucu AYAKTA ama art arda bu kadar parçada (yeniden
# denemeler tükendikten sonra) HTTP hatası döndürdüyse devre açılır
# (MSG_SERVER_FAILING). Tek paragrafa özgü kalıcı 500 (o girdide çöken model
# çalıştırıcısı) öğe hatasıdır; bitişik birkaç "zehirli" paragraf koşuyu
# durdurmaz. Bağlantı düzeyi sayaç (MT_HTTP_CIRCUIT_MAX_FAILURES) ayrıdır.
# Yamalanabilir (çağrı anında ``backends.MT_HTTP_SERVER_ERROR_MAX_ITEMS``).
# NOT: config/defaults.py'ye taşınmalı (bu dalgada dondurulmuş dosya).
MT_HTTP_SERVER_ERROR_MAX_ITEMS = 5


# ---------------------------------------------------------------------------
# Yardımcılar
# ---------------------------------------------------------------------------
def short_detail(text: Any, limit: int = _DETAIL_MAX_CHARS) -> str:
    """Dış kaynaklı hata metnini iletiye göm: tek satır, kısa, OOM alt dizgisiz."""
    flat = " ".join(str(text or "").split())
    if len(flat) > limit:
        flat = flat[:limit].rstrip() + "…"
    return oom_safe(flat)


def format_seconds(seconds: float) -> str:
    """240.0 -> "240"; 0.5 -> "0,5" (Türkçe ondalık)."""
    value = float(seconds)
    if value.is_integer():
        return str(int(value))
    return f"{value:.1f}".replace(".", ",")


def estimate_tokens(chars: int) -> int:
    """Karakter sayısından kaba token tahmini (``_scale_max_tokens`` ile aynı varsayım)."""
    if chars <= 0:
        return 0
    return int(math.ceil(chars / MT_CONTEXT_CHARS_PER_TOKEN))


def output_token_limit(chars: int, floor: int = MT_OUTPUT_TOKENS_FLOOR) -> int:
    """Üretim üst sınırı (KARARLAR 15) — bütün arka uçların TEK hesabı.

    ``kaynak karakter / MT_CONTEXT_CHARS_PER_TOKEN × MT_OUTPUT_TOKENS_FACTOR``;
    en az ``floor`` (varsayılan MT_OUTPUT_TOKENS_FLOOR), en çok
    ``MT_OUTPUT_TOKENS_CAP``. Sınır bir ÜST sınırdır: kısa girdide model EOS'ta
    erken durur; taban kısır döngü frenidir.
    """
    est_tokens = max(0, int(chars or 0)) / MT_CONTEXT_CHARS_PER_TOKEN
    scaled = int(est_tokens * MT_OUTPUT_TOKENS_FACTOR)
    return max(int(floor), min(scaled, MT_OUTPUT_TOKENS_CAP))


def near_output_limit(output_tokens: Optional[int], limit: int) -> bool:
    """Üretilen token sınırın ``MT_OUTPUT_NEAR_LIMIT_RATIO`` oranına vardı mı.

    ``output_tokens`` bilinmiyorsa (None) False: karar yalnız gerçek sayaçla.
    """
    if output_tokens is None or not limit or limit <= 0:
        return False
    return int(output_tokens) >= MT_OUTPUT_NEAR_LIMIT_RATIO * int(limit)


def context_error(prompt_chars: int, max_tokens: int, ctx: Optional[int]) -> Optional[str]:
    """Tahmini prompt + üretim bütçesi bağlamı aşıyorsa Türkçe öğe hatası, değilse None.

    Katı (eski) denetim: üretim ÜST sınırını da bağlama sayar. Arka uçlar
    2026-09-29'dan beri ``context_budget`` + ``context_overflow`` kullanır
    (F4-03: üst sınır sığmasa da çeviri sığabilir); bu fonksiyon geriye dönük
    uyumluluk için durur.
    """
    if not ctx or ctx <= 0:
        return None
    need = estimate_tokens(prompt_chars) + max(0, int(max_tokens or 0))
    if need > ctx:
        return MSG_CONTEXT_EXCEEDED.format(need=need, ctx=ctx)
    return None


def context_budget(prompt_chars: int, max_tokens: int,
                   ctx: Optional[int]) -> Tuple[int, Optional[str]]:
    """Bağlama göre üretim sınırı: ``(sınır, öğe hatası ya da None)``.

    2026-09-29 (F4-03): üretim sınırı (``output_token_limit``) bir ÜST
    sınırdır (0,73 token/karakter); gerçek Türkçe çıktı çoğunlukla çok daha
    kısadır (ölçülen ~0,45). Eskiden istem + üst sınır bağlamı aşınca istek
    hiç gönderilmiyordu: 2048 bağlamlı sunucuda sığacak paragraf İngilizce
    kalıyordu. Şimdi:

    - istem (tahmini, ``estimate_tokens``) tek başına bağlamı dolduruyorsa
      istek gönderilmez, ``MSG_CONTEXT_EXCEEDED`` (segment özgün kalır, QC
      görür — TASARIM B.5);
    - aksi hâlde sınır bağlamda kalan yere kısılır: ``min(sınır, bağlam -
      istem)``. Çıktı kısılan sınıra dayanırsa ``context_overflow`` sonucu
      öğe hatasına çevirir: kesik çeviri ASLA kabul edilmez (kayıpsızlık).

    Bağlam bilinmiyorsa (None / 0) sınır aynen döner.
    """
    requested = max(0, int(max_tokens or 0))
    if not ctx or ctx <= 0:
        return requested, None
    prompt = estimate_tokens(prompt_chars)
    room = int(ctx) - prompt
    if room <= 0:
        return requested, MSG_CONTEXT_EXCEEDED.format(need=prompt + requested, ctx=ctx)
    return min(requested, room), None


def context_overflow(result: GenerationResult, limit: int, requested: int,
                     prompt_chars: int, ctx: Optional[int]) -> GenerationResult:
    """Sınır bağlam yüzünden kısıldıysa ve çıktı o sınıra dayandıysa öğe hatası.

    ``context_budget``'ın ikinci yarısı (F4-03): kısılmış sınırda
    ``finish_reason == "length"`` (ya da sayılan yanıt token'ı sınıra vardı)
    çevirinin kesildiği demektir; metin atılır, ``MSG_CONTEXT_EXCEEDED`` döner
    (segment özgün kalır). Sınır kısılmadıysa sonuç aynen döner (sınıra dayanan
    çıktının uyarısı çevirmenin işidir, KARARLAR 15).
    """
    if not ctx or limit >= requested or result.error:
        return result
    used = result.output_tokens
    if result.finish_reason == "length" or (used is not None and used >= limit):
        logger.warning("[MT ARKA UÇ] Çıktı bağlamda kalan %d token'a sığmadı (bağlam %s); "
                       "kesik çeviri kabul edilmedi, segment özgün kalıyor", limit, ctx)
        return dataclasses.replace(result, text="", error=MSG_CONTEXT_EXCEEDED.format(
            need=estimate_tokens(prompt_chars) + int(requested), ctx=ctx))
    return result


# MSG_CONTEXT_EXCEEDED kalıbının sabit başı ("Segment modelin bağlam sınırını aşıyor")
_CONTEXT_ERROR_HEAD = MSG_CONTEXT_EXCEEDED.split("(", 1)[0].strip()


def is_context_error(error: Any) -> bool:
    """Öğe hatası bağlam aşımı mı (``MSG_CONTEXT_EXCEEDED``)?

    Çevirmen bu parçayı daha küçük parçalara bölüp yeniden deneyebilir
    (F4-03, çevirmen kulvarı için sözleşme).
    """
    return isinstance(error, str) and error.startswith(_CONTEXT_ERROR_HEAD)


def _positive_int(value: Any) -> Optional[int]:
    if isinstance(value, bool):
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _positive_float(value: Any) -> Optional[float]:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 and math.isfinite(number) else None


def _number(value: Any, default: float) -> float:
    if isinstance(value, bool):
        return default
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


# ---------------------------------------------------------------------------
# Düşünme ölçümü ve bütçesi (2026-09-28, KARARLAR 16)
# ---------------------------------------------------------------------------
# Sohbet şablonunda (GGUF tokenizer.chat_template, MLX tokenizer) düşünme
# belirtisi. discovery._THINK_MARKERS ile aynı iki işaret + gpt-oss harmony
# kanalı (ham llama.cpp çıktısında kanallar metne düşer).
_TEMPLATE_THINK_MARKERS = ("<think>", "enable_thinking", HARMONY_MARK)
# Düşünme bölgesindeki işaretleme (düşünme etiketi, harmony özel token'ı):
# ``ThinkingMeter.substantive`` bunları çıkarıp boşluk dışı içerik arar (G2b-1).
_THINK_MARKUP_RE = re.compile(r"</?think>|<\|[A-Za-z_]+\|>", re.IGNORECASE)
# Qwen3 "/no_think" yumuşak anahtarı yalnız Qwen3 karma (hybrid) modellerinde
# belgeli (Hugging Face Qwen/Qwen3-8B model kartı). "Qwen3.5" gibi sonraki
# nesiller ve 2507 sürümleri (yalnız düşünen ya da hiç düşünmeyen) DIŞARIDA.
# G2b-1 belge denetimi (2026-10-05): Qwen3.5 kartı "does not officially support
# the soft switch of Qwen3, i.e., /think and /nothink" der; kapatma orada
# enable_thinking=False ile (https://huggingface.co/Qwen/Qwen3.5-35B-A3B).
# qwen3.x dışlaması belgeyle uyumlu; LM Studio'daki qwen3.x için resmî yol
# yerel REST reasoning "off"tur (core/mt_engines/thinking.py).
_QWEN3_HYBRID_RE = re.compile(r"(?i)qwen[-_ ]?3(?![.\d])")
_QWEN3_EXCLUDE_RE = re.compile(r"2507")
# Koşu istatistiği anahtarları (ThinkingGuard.stats -> BookTranslator.stats ->
# latex_pipeline TranslationStats): düşünme görülen parça; akış parçası /
# sunucu sayacıyla sayılan ve sayaç yokken karakterden tahmin edilen düşünme
# token'ı; bütçeyi / payı aşan deneme (akışta hemen kesilir; akışsız ilk
# yanıtta kesme yoktur, yalnız sayılır); sert (kapatan ya da daha kısa
# düşünen) yolla yeniden deneme — düşünerek çeviride AYNI istekle yeniden
# deneme sayılmaz; düşünme sınırı yüzünden çevrilmeyen parça (düşünerek
# çeviride pay ya da süre dolup cevapsız kalan parça dahil, G2b-2).
THINK_STAT_KEYS = ("thinking_segments", "thinking_tokens", "thinking_tokens_estimated",
                   "think_budget_cuts", "think_retries", "think_failed")


def template_signals_thinking(template: Any) -> bool:
    """Sohbet şablonu düşünen model belirtisi taşıyor mu (``<think>``, ``enable_thinking``, harmony)."""
    if isinstance(template, str):
        return any(marker in template for marker in _TEMPLATE_THINK_MARKERS)
    if isinstance(template, list):   # [{"name": ..., "template": ...}]
        return any(template_signals_thinking(item.get("template"))
                   for item in template if isinstance(item, dict))
    return False


# Üretim istemi açık bir <think> ile bitiyor: şablon düşünmeyi istemin içinde
# açmış (DeepSeek-R1 damıtmaları, QwQ, Qwen3 *-Thinking-2507 şablonları). Model
# yanıta <think> yazmadan düşünür; düşünme ilk </think>'e kadar sürer.
_PROMPT_OPEN_THINK_RE = re.compile(r"<think>\s*$", re.IGNORECASE)


def prompt_opens_thinking(prompt: Any) -> bool:
    """Üretim istemi açık bir ``<think>`` ile mi bitiyor (zorla açık düşünme)?

    2026-09-28 (VTD): bu durumda yanıtın başı ``</think>``'e kadar düşünmedir;
    ``ThinkingMeter(forced_open=True)`` bütçeyi ilk parçadan işletir. Aksi
    hâlde düşünme metni "yanıt" sayılıyor, bütçe hiç işlemiyor ve kapanmayan
    düşünme çeviri diye kabul ediliyordu.
    """
    return isinstance(prompt, str) and bool(_PROMPT_OPEN_THINK_RE.search(prompt))


def template_opens_thinking(template: Any) -> bool:
    """GGUF sohbet şablonu üretim isteminin sonunda ``<think>`` açıyor mu?

    Şablon tek kullanıcı mesajıyla ``add_generation_prompt=True`` işlenir;
    ortam llama-cpp-python'ın ``Jinja2ChatFormatter``'ıyla aynı kurallardadır
    (korumalı ortam, ``trim_blocks`` / ``lstrip_blocks``, ``loopcontrols``,
    ``tojson``, ``raise_exception``, ``strftime_now``). İşlenemezse False
    (belirti yok sayılır; akış yine ölçer). ``jinja2`` yalnız burada içe
    aktarılır (modül hafif kalır).
    """
    if isinstance(template, list):   # [{"name": ..., "template": ...}] -> varsayılan
        chosen = next((item.get("template") for item in template
                       if isinstance(item, dict) and item.get("name") == "default"), None)
        template = chosen
    if not isinstance(template, str) or not THINK_OPEN_RE.search(template):
        return False
    try:
        import jinja2
        import jinja2.ext
        from jinja2.sandbox import ImmutableSandboxedEnvironment
    except ImportError:
        return False

    def _raise(message: str) -> None:
        raise jinja2.exceptions.TemplateError(message)

    try:
        env = ImmutableSandboxedEnvironment(loader=jinja2.BaseLoader(), trim_blocks=True,
                                            lstrip_blocks=True,
                                            extensions=[jinja2.ext.loopcontrols])
        env.filters["tojson"] = lambda value, **_kw: json.dumps(value, ensure_ascii=False)
        rendered = env.from_string(template).render(
            messages=[{"role": "user", "content": "x"}], add_generation_prompt=True,
            bos_token="", eos_token="", raise_exception=_raise,
            strftime_now=lambda fmt: "")
    except Exception:   # şablon işlenemedi: belirti yok
        return False
    return prompt_opens_thinking(rendered)


def inline_thinking(text: str) -> Tuple[int, bool]:
    """Metin içi düşünme: (düşünmenin bittiği karakter konumu, düşünme hâlâ açık mı).

    - harmony: son ``final`` kanalına kadarı düşünmedir; ``final`` yoksa hepsi (açık);
    - ``<think>`` ile başlıyorsa ilk ``</think>``'e kadar (kapanmadıysa hepsi, açık);
    - ``<think>`` yok ama ``</think>`` var (şablon düşünmeyi istemin içinde
      açmış): ``</think>``'e kadarı düşünmedir;
    - hiçbiri yoksa (0, False).
    Dönen konum içerikte yanıtın başladığı yerdir (``text[konum:]`` = yanıt).
    """
    if not text:
        return 0, False
    if HARMONY_MARK in text:
        finals = list(HARMONY_FINAL_RE.finditer(text))
        if not finals:
            return len(text), True
        return finals[-1].end(), False
    close = THINK_CLOSE_RE.search(text)
    if THINK_OPEN_RE.match(text.lstrip()):
        if close is None:
            return len(text), True
        return close.end(), False
    if close is not None:
        return close.end(), False
    return 0, False


class ThinkingMeter:
    """Tek bir üretim denemesinin düşünme ölçümü (gerçek sayım; tahmin işaretlenir).

    Akışta parça parça beslenir (``add_thinking`` / ``add_content``; her akış
    parçası bir token sayılır — Ollama, llama.cpp ve mlx_lm akışı token başına
    parça gönderir). Akışsız yanıtta ``add_whole`` bir kez çağrılır: sunucu
    düşünme sayacı verdiyse o kullanılır, yoksa düşünme metninin karakterinden
    ``estimate_tokens`` ile tahmin edilir (``estimated`` True).
    ``content`` ham içeriktir (metin içi düşünme blokları dahil; temizliği
    motorun ``clean_output``'u yapar).

    ``forced_open``: şablon düşünmeyi üretim isteminin içinde açtı
    (``prompt_opens_thinking`` / ``template_opens_thinking``). İçerik ilk
    ``</think>``'e kadar düşünmedir: bütçe ilk parçadan işler, kapanmayan
    düşünme hiçbir zaman yanıt sayılmaz (VTD 2026-09-28).

    Süre (G2b-2): akışta ölçer nesnesinin kuruluşundan (istek başı) son
    düşünme parçasına kadar geçen GERÇEK süre (``thinking_seconds``); akışsız
    yanıtta düşünmenin süresi ayrıca bilinmez, None (tahmin yazılmaz).
    """

    def __init__(self, budget: int, source: str = "", forced_open: bool = False):
        self.budget = max(0, int(budget or 0))
        self.tokens = 0              # düşünme token'ı
        self.chars = 0               # düşünme metni karakteri
        self.estimated = False
        self._t0 = time.monotonic()
        self._last_think_t: Optional[float] = None   # son düşünme parçasının anı (akış)
        self._streamed = False
        # Ayrı düşünme alanında boşluk dışı metin geldi mi (G2b-1; ``substantive``)
        self._field_substance = False
        self.answer_tokens = 0       # akışta yanıt (düşünme dışı) parçası
        self.content = ""
        self._inline_ok = not source_has_think(source)
        self._inline_end = 0         # içerikte düşünmenin bittiği konum
        self._inline_open = False
        # Zorla açık düşünme (kaynakta düşünme işareti varsa ayrıştırma yok: kapalı)
        self._forced_open = bool(forced_open) and self._inline_ok
        if self._forced_open:
            self._inline_open = True

    # ---------------------------------------------------------------- durum
    @property
    def exceeded(self) -> bool:
        """Düşünme bütçeyi aştı mı (token > bütçe)."""
        return self.budget > 0 and self.tokens > self.budget

    @property
    def thinking_open(self) -> bool:
        """Metin içi düşünme bloğu kapanmadı mı."""
        return self._inline_open

    def answer_text(self) -> str:
        """İçeriğin düşünme dışındaki kısmı (yanıt); düşünme açıksa boş."""
        if self._inline_open:
            return ""
        return self.content[self._inline_end:]

    @property
    def substantive(self) -> bool:
        """Düşünmede etiket dışı gerçek içerik var mı (G2b-1, 2026-10-05).

        Boş ``<think>\\n\\n</think>`` bloğu (Qwen3 "/no_think" yanıtı, kapatılmış
        şablonun yankısı) düşünce sayılmaz. Sunucu düşünme sayacı verip metin
        vermediyse sayaca güvenilir. Ölçümü (``tokens``/``exceeded``) DEĞİŞTİRMEZ;
        yalnız kapatma doğrulaması (``thinking.ThinkingOffMonitor``) okur.
        """
        if self._field_substance:
            return True
        region = self.content if self._inline_open else self.content[:self._inline_end]
        if region and _THINK_MARKUP_RE.sub("", region).strip():
            return True
        return self.tokens > 0 and not self.chars

    @property
    def thinking_seconds(self) -> Optional[float]:
        """İstek başından son düşünme parçasına kadar ölçülen süre (sn); akışta
        düşünme görülmediyse ya da yanıt akışsızsa None (G2b-2)."""
        if not self._streamed or self.tokens <= 0 or self._last_think_t is None:
            return None
        return max(0.0, self._last_think_t - self._t0)

    def _mark_thinking(self) -> None:
        self._last_think_t = time.monotonic()

    # ---------------------------------------------------------------- akış
    def add_thinking(self, text: Any, tokens: int = 1) -> None:
        """Ayrı düşünme alanından bir akış parçası (Ollama message.thinking,
        OpenAI uyumlu reasoning_content / reasoning)."""
        if not isinstance(text, str) or not text:
            return
        self._streamed = True
        self.chars += len(text)
        self.tokens += max(0, int(tokens))
        self._mark_thinking()
        if text.strip():
            self._field_substance = True

    def add_content(self, text: Any, tokens: int = 1) -> None:
        """Yanıt içeriğinden bir akış parçası (metin içi düşünme ayrıştırılır)."""
        if not isinstance(text, str) or not text:
            return
        self._streamed = True
        self.content += text
        if self._forced_open:
            # İstemde açılmış düşünme: ilk </think>'e kadar her parça düşünmedir.
            close = THINK_CLOSE_RE.search(self.content)
            self._mark_thinking()
            if close is None:
                self.chars += len(text)
                self.tokens += max(0, int(tokens))
                return
            # </think> bu parçada geldi: parçanın kapanışa kadarki kısmı düşünme,
            # sonrası yanıt (parça etiketi taşıdığı için düşünme token'ı sayılır).
            before = len(self.content) - len(text)
            self.chars += max(0, close.end() - before)
            self.tokens += max(0, int(tokens))
            self._forced_open = False
            self._inline_open = False
            self._inline_end = close.end()
            return
        if not self._inline_ok or (self._inline_end and not self._inline_open):
            # Kaynakta düşünme işareti var ya da düşünme bloğu kapandı: yanıt parçası
            self.answer_tokens += max(0, int(tokens))
            return
        end, is_open = inline_thinking(self.content)
        if end > self._inline_end:
            if (self._inline_end == 0 and not is_open and self.answer_tokens
                    and not THINK_OPEN_RE.match(self.content.lstrip())
                    and HARMONY_MARK not in self.content):
                # Zorla açık düşünme: </think> geldi, önceki parçalar da düşünmeymiş.
                self.tokens += self.answer_tokens
                self.answer_tokens = 0
            self.chars += end - self._inline_end
            self.tokens += max(0, int(tokens))
            self._inline_end = end
            self._mark_thinking()
        else:
            self.answer_tokens += max(0, int(tokens))
        self._inline_open = is_open

    # ---------------------------------------------------------------- akışsız
    def add_whole(self, thinking_text: Any, content: Any,
                  thinking_tokens: Optional[int] = None) -> None:
        """Akışsız yanıtın tamamı: ayrı düşünme alanı + içerik (+ sunucu sayacı)."""
        self._streamed = False
        self.content = content if isinstance(content, str) else ""
        field_chars = len(thinking_text) if isinstance(thinking_text, str) else 0
        self._field_substance = isinstance(thinking_text, str) and bool(thinking_text.strip())
        end, is_open = inline_thinking(self.content) if self._inline_ok else (0, False)
        if self._forced_open and THINK_CLOSE_RE.search(self.content) is None:
            end, is_open = len(self.content), True   # istemde açılan düşünme kapanmadı
        self._forced_open = False
        self._inline_end, self._inline_open = end, is_open
        self.chars = field_chars + end
        counted = _positive_int(thinking_tokens)
        if counted is not None:
            self.tokens = counted
        elif self.chars:
            self.tokens = estimate_tokens(self.chars)
            self.estimated = True

    def result_fields(self) -> Dict[str, Any]:
        """GenerationResult'a aktarılacak ölçüm alanları."""
        return {"thinking_tokens": self.tokens, "thinking_estimated": self.estimated,
                "thinking_exceeded": self.exceeded,
                "thinking_substantive": self.tokens > 0 and self.substantive,
                "think_budget": self.budget, "thinking_seconds": self.thinking_seconds}


def resolve_think_budget(spec) -> int:
    """Parça başına düşünme bütçesi: ``options.think_budget`` (pozitif tamsayı)
    ya da ``MT_THINK_BUDGET_TOKENS``. Geçersiz değer uyarıyla yok sayılır."""
    options = getattr(spec, "options", None)
    raw = options.get("think_budget") if isinstance(options, dict) else None
    if raw is None:
        return int(MT_THINK_BUDGET_TOKENS)
    value = None if isinstance(raw, float) and not raw.is_integer() else _positive_int(raw)
    if value is None:
        logger.warning("[MT ARKA UÇ] think_budget geçersiz (%r); varsayılan %d token "
                       "kullanılıyor", raw, int(MT_THINK_BUDGET_TOKENS))
        return int(MT_THINK_BUDGET_TOKENS)
    return value


def resolve_think_cap(spec) -> int:
    """Düşünerek çeviride parça payının tavanı (G2b-2): ``options.think_budget``
    (pozitif tamsayı; kayıt sahibinin "parça başına en çok düşünme" tercihi)
    ya da ``MT_THINK_ALLOWANCE_CAP_TOKENS``. Geçersiz değerin uyarısını
    ``resolve_think_budget`` yazar (iki kez yazılmaz)."""
    options = getattr(spec, "options", None)
    raw = options.get("think_budget") if isinstance(options, dict) else None
    if raw is None:
        return int(MT_THINK_ALLOWANCE_CAP_TOKENS)
    value = None if isinstance(raw, float) and not raw.is_integer() else _positive_int(raw)
    return value if value is not None else int(MT_THINK_ALLOWANCE_CAP_TOKENS)


def think_allowance(chars: int, cap: Optional[int] = None) -> int:
    """Düşünerek çeviride parçanın düşünme payı (KARARLAR 22) — TEK hesap.

    ``MT_THINK_ALLOWANCE_BASE_TOKENS + ⌈kaynak karakter × MT_THINK_ALLOWANCE_PER_CHAR⌉``,
    en çok *cap* (verilmezse ``MT_THINK_ALLOWANCE_CAP_TOKENS``). Sayıların
    gerekçesi ve güven düzeyi config/defaults.py'de.
    """
    scaled = int(math.ceil(max(0, int(chars or 0)) * float(MT_THINK_ALLOWANCE_PER_CHAR)))
    limit = int(cap) if cap else int(MT_THINK_ALLOWANCE_CAP_TOKENS)
    return max(1, min(int(MT_THINK_ALLOWANCE_BASE_TOKENS) + scaled, limit))


def short_item(chars: int) -> bool:
    """Kısa parça mı (başlık gibi; devre kesicinin ayrı penceresi, G2b-2)."""
    return int(chars or 0) <= int(MT_THINK_BREAKER_SHORT_CHARS)


# MSG_TIMEOUT kalıbını tanıyan düzenli ifade ("Model 240 sn içinde yanıt vermedi.")
_TIMEOUT_ERROR_RE = re.compile(
    "^" + re.escape(MSG_TIMEOUT).replace(re.escape("{seconds}"), r"[\d.,]+") + "$")


def is_timeout_error(error: Any) -> bool:
    """Öğe hatası zaman aşımı mı (``MSG_TIMEOUT``)?"""
    return isinstance(error, str) and bool(_TIMEOUT_ERROR_RE.match(error))


def is_no_answer(result: Any) -> bool:
    """Deneme "cevapsız" mı (G2b-2): model düşünme sınırına dayanıp cevap
    üretmedi (``thinking_exceeded`` + hata) ya da düşünürken süresi doldu
    (zaman aşımı + ölçülen düşünme). Başka öğe hataları (bağlam aşımı, sunucu
    hatası, boş çıktı) cevapsızlık sayılmaz: devre kesiciye girmez."""
    if not isinstance(result, GenerationResult) or not result.error:
        return False
    if result.thinking_exceeded:
        return True
    return is_timeout_error(result.error) and result.thinking_tokens > 0


def _recommended_model_label() -> str:
    """Önerilen yerel model (KARARLAR 29): registry'deki varsayılan motorun
    ekrandaki adı (``discovery.engine_label``; "HY-MT1.5 7B (MLX)")."""
    try:
        from . import registry
    except Exception:   # içe aktarılamazsa ileti yine üretilir
        return ""
    try:
        from .discovery import engine_label
        return engine_label(registry.resolve_spec(registry.DEFAULT_ENGINE))
    except Exception:   # etiket üretilemezse kimlik
        return str(registry.DEFAULT_ENGINE)


def thinking_no_answer_reason(count: int, window: int) -> str:
    """Düşünme devre kesicisinin Türkçe nedeni (metin ``ui.branding``'de; G2b-2).

    *count* / *window*: kayan penceredeki GERÇEK cevapsız / örnek sayısı.
    """
    from ui.branding import THINKING_NO_ANSWER_MESSAGE

    return oom_safe(THINKING_NO_ANSWER_MESSAGE.format(
        window=int(window), count=int(count), recommended=_recommended_model_label()))


def _answer_key(instruction: str, text: str) -> str:
    """Aynı isteğin (talimat + metin) koşu içi kimliği (cevapsız istek belleği)."""
    data = f"{instruction or ''}\x00{text or ''}".encode("utf-8", "replace")
    return hashlib.sha1(data).hexdigest()


def _seconds_text(seconds: Optional[float]) -> str:
    """Günlük için ölçülen süre: "3,2 sn"; ölçülmediyse açıkça söyler."""
    if seconds is None:
        return "süre ölçülmedi (akışsız yanıt)"
    return f"{seconds:.1f}".replace(".", ",") + " sn"


def wants_thinking_off(spec) -> bool:
    """Motor düşünmeyi kapatmak istiyor mu (EngineSpec.think False, varsayılan).

    ``think: null`` (dokunma) ya da ``true`` verilen kayıtta sert kapatmalı
    yeniden deneme YAPILMAZ (kullanıcının açık tercihi); bütçe yine işler.
    """
    return getattr(spec, "think", False) is False


def supports_no_think(spec) -> bool:
    """Model Qwen3 karma ailesinden mi ("/no_think" yumuşak anahtarı belgeli)."""
    names = [getattr(spec, "model", ""), getattr(spec, "local_dir", ""),
             os.path.basename(str(getattr(spec, "path", "") or "")), getattr(spec, "name", "")]
    for name in names:
        if isinstance(name, str) and name and _QWEN3_HYBRID_RE.search(name) \
                and not _QWEN3_EXCLUDE_RE.search(name):
            return True
    return False


def with_no_think(messages: Sequence[Dict[str, str]]) -> List[Dict[str, str]]:
    """Mesajlara "/no_think" anahtarını sistem mesajı olarak ekle (yeni liste)."""
    out = [dict(m) for m in messages]
    if out and out[0].get("role") == "system":
        head = str(out[0].get("content") or "").rstrip()
        out[0]["content"] = f"{head}\n{NO_THINK_SWITCH}" if head else NO_THINK_SWITCH
    else:
        out.insert(0, {"role": "system", "content": NO_THINK_SWITCH})
    return out


class ThinkingGuard:
    """Düşünme politikası: yoklama bütçesi, düşünerek çeviri payı, TEK yeniden
    deneme, ölçülen hız ve koşu devre kesicisi (KARARLAR 16 + 22).

    ``run(attempt, can_harden, thinking_mode=..., short=..., shorter=...,
    chars=...)``: ``attempt(hard)`` bir üretim denemesi yapar (çağıran bütçe ve
    süreyi ``plan`` ile kurar; ``thinking_mode()`` her denemeden önce okunur).

    - Düşünerek çeviri kipi (kapatma tutmadığı anda yapışkan bayrakla açılır;
      bayrağın konduğu denemenin yeniden denemesi bile kiptedir): ilk deneme
      tam payla ve sert kip OLMADAN gider. Pay dolup cevap gelmezse
      (``is_no_answer``) parça BİR KEZ (``MT_THINK_RETRY_COUNT``) yeniden
      denenir — resmî daha kısa düşünme yolu varsa (``shorter()``: Ollama
      "low", llama.cpp server ``reasoning_effort: none``, Qwen3 "/no_think",
      LM Studio REST ``reasoning: low``) onunla (sert kip), yoksa aynı istekle.
      Yine cevap yoksa sonuç ``MSG_THINK_BUDGET`` (ya da zaman aşımı) öğe
      hatasıdır: parça özgün dilinde kalır, koşu sürer. Sert yol düşünmeyi
      gerçekten KAPATTIYSA (yanıtta gerçek düşünce yok: Qwen3 "/no_think",
      ``reasoning_effort: none``) yapışkan olur — KARARLAR 22 "kapatılabiliyorsa
      kapatılır"; yalnız kısaltan yol ("low") yapışkan değildir, her parça
      önce tam düşünür.
    - Kip dışında (düşünmesi kapalı sayılan model) eski davranış: bütçeyi aşan
      deneme sert kapatma yolu varsa bir kez sert kipte yinelenir; işe yararsa
      sert kip YAPIŞKAN olur (sonraki parçalar doğrudan sert kiple gider).

    Devre kesici (``note_segment``): yalnız kipteki parçalar; koşu BOYUNCA
    kayan pencere (son ``MT_THINK_BREAKER_WINDOW`` parça, en az
    ``MT_THINK_BREAKER_MIN_SAMPLES`` örnek, cevapsız oranı
    ``MT_THINK_BREAKER_RATIO``); kısa parçalar (``short_item``) ayrı pencerede.
    Açılınca ``tripped`` Türkçe nedeni taşır (``thinking_no_answer_reason``).
    RAPOR MT-13: eski pencere yalnız koşunun ilk 5 çağrısına bakıyordu.

    ``stats``: koşu boyu ölçüm sayaçları (``THINK_STAT_KEYS``; yalnız gerçek
    sayım — akış parçası / sunucu sayacı ile karakterden tahmin ayrı tutulur).
    Hız: kipte akışla ölçülen düşünme token/sn (``record_rate``). Cevapsız
    kalan istek koşu boyunca hatırlanır (``cached_no_answer``). İş parçacığı
    güvenlidir (toplu üretim havuzu).
    """

    def __init__(self, budget: int, cap: Optional[int] = None):
        self.budget = int(budget)
        self.cap = int(cap) if cap else int(MT_THINK_ALLOWANCE_CAP_TOKENS)
        self.hard_mode = False
        self.seen = False            # herhangi bir yanıtta düşünme ölçüldü
        self.tripped: Optional[str] = None
        self.stats: Dict[str, int] = {key: 0 for key in THINK_STAT_KEYS}
        self._lock = threading.Lock()
        # Kayan pencereler: kısa parça (True) / gövde (False); öğe = cevapsız mı
        self._windows: Dict[bool, List[bool]] = {False: [], True: []}
        self._rate_tokens = 0
        self._rate_seconds = 0.0
        self._mode_logged = False    # kip satırı bir kez
        self._seen_logged = False    # "model düşünüyor" satırı bir kez
        self._no_answer: Dict[str, GenerationResult] = {}

    # ---------------------------------------------------- pay, hız, süre planı
    def allowance(self, chars: int) -> int:
        """Düşünerek çeviride bu uzunluktaki parçanın düşünme payı (token)."""
        return think_allowance(chars, self.cap)

    def attempt_budget(self, chars: int, thinking_mode: bool) -> int:
        """Denemenin düşünme sınırı: kipte pay, değilse yoklama bütçesi."""
        return self.allowance(chars) if thinking_mode else self.budget

    def record_rate(self, result: Any) -> None:
        """Akışla ölçülen düşünme hızını biriktir (en az
        ``MT_THINK_RATE_MIN_TOKENS`` sayılmış token; tahmin ya da süresiz sonuç girmez)."""
        seconds = getattr(result, "thinking_seconds", None)
        tokens = int(getattr(result, "thinking_tokens", 0) or 0)
        if (seconds is None or seconds <= 0 or getattr(result, "thinking_estimated", False)
                or tokens < int(MT_THINK_RATE_MIN_TOKENS)):
            return
        with self._lock:
            self._rate_tokens += tokens
            self._rate_seconds += float(seconds)

    def measured_rate(self) -> Optional[float]:
        """Koşuda ÖLÇÜLEN düşünme hızı (token/sn); ölçüm yoksa None."""
        with self._lock:
            if self._rate_tokens > 0 and self._rate_seconds > 0:
                return self._rate_tokens / self._rate_seconds
        return None

    def rate(self) -> float:
        """Düşünme hızı (token/sn): koşuda ölçülen; ölçüm yoksa varsayılan."""
        measured = self.measured_rate()
        return measured if measured is not None else float(MT_THINK_ASSUMED_TOKENS_PER_S)

    def item_seconds(self, chars: int, out_limit: int, ceiling: float) -> float:
        """Kipte bir parçanın (bütün denemeleri) süre sınırı: paya ve hıza göre
        ölçekli (config/defaults.py formülü), en çok *ceiling* (istek zaman aşımı)."""
        attempts = 1 + max(0, int(MT_THINK_RETRY_COUNT))
        tokens = max(0, int(out_limit or 0)) + attempts * self.allowance(chars)
        scaled = float(MT_THINK_TIMEOUT_BASE_S) + tokens / self.rate() * float(MT_THINK_TIMEOUT_SAFETY)
        return min(float(ceiling), scaled)

    def plan(self, chars: int, out_limit: int, thinking_mode: bool,
             ceiling: float) -> Tuple[int, float]:
        """Denemenin ``(düşünme sınırı token, öğe başından itibaren süre sınırı sn)``.

        Kip dışında eski davranış: yoklama bütçesi ve bütün denemelerin
        paylaştığı *ceiling* (istek zaman aşımı). Kipte süre ``item_seconds``
        (paya ve hıza göre, en çok *ceiling*), bütün denemeler paylaşır; pay
        normalde süreden önce dolar (süre, deneme sayısı kadar payı yarı hızda
        karşılar). Tavan bağlıyorsa (yavaş model) ve hız ÖLÇÜLDÜYSE pay, deneme
        sayısı kadar payın + çeviri sınırının süreye sığacağı kadara kısılır:
        tek yeniden denemeye de süre kalır. Ölçüm yokken pay kısılmaz (varsayılan
        hız gerçek hızlı modeli cezalandırmasın).
        """
        if not thinking_mode:
            return self.budget, float(ceiling)
        item = self.item_seconds(chars, out_limit, ceiling)
        allowance = self.allowance(chars)
        rate = self.measured_rate()
        if rate is not None:
            attempts = 1 + max(0, int(MT_THINK_RETRY_COUNT))
            room = rate * max(0.0, item - float(MT_THINK_TIMEOUT_BASE_S)) \
                - max(0, int(out_limit or 0))
            allowance = max(1, min(allowance, int(room // attempts)))
        return allowance, item

    # ---------------------------------------------------- cevapsız istek belleği
    def cached_no_answer(self, instruction: str, text: str) -> Optional[GenerationResult]:
        """Bu istek bu koşuda kipte cevapsız kaldıysa o sonuç (modele gidilmez)."""
        with self._lock:
            hit = self._no_answer.get(_answer_key(instruction, text))
        if hit is not None:
            logger.info("[MT DÜŞÜNME] Bu parça bu koşuda düşünme payını doldurup cevapsız "
                        "kalmıştı; aynı istek modele yeniden gönderilmedi (açgözlü üretimde "
                        "sonuç değişmez): %.60s", text or "")
        return hit

    def adopt_hard_mode(self, why: str) -> None:
        """Sert kipi (resmî kapatma yolu) baştan yapışkan aç; günlüğe bir satır."""
        with self._lock:
            if self.hard_mode:
                return
            self.hard_mode = True
        logger.info("[MT DÜŞÜNME] %s", why)

    def remember_no_answer(self, instruction: str, text: str, result: GenerationResult) -> None:
        """Pay dolup cevapsız kalan isteği hatırla. Zaman aşımıyla cevapsız kalan
        hatırlanmaz (sunucunun anlık yavaşlığı olabilir; sonraki çağrı yine dener)."""
        if not (result.thinking_exceeded and result.error):
            return
        with self._lock:
            self._no_answer[_answer_key(instruction, text)] = result

    # ---------------------------------------------------- iç
    def _observe(self, result: GenerationResult) -> None:
        if result.thinking_tokens > 0 and not self.seen:
            with self._lock:
                self.seen = True
        self.record_rate(result)

    def _record(self, attempt_results: List[GenerationResult], final: GenerationResult,
                retries: int, failed: bool) -> None:
        counted = sum(r.thinking_tokens for r in attempt_results if not r.thinking_estimated)
        estimated = sum(r.thinking_tokens for r in attempt_results if r.thinking_estimated)
        with self._lock:
            st = self.stats
            if counted or estimated:
                st["thinking_segments"] += 1
            st["thinking_tokens"] += counted
            st["thinking_tokens_estimated"] += estimated
            st["think_budget_cuts"] += sum(int(r.thinking_exceeded) for r in attempt_results)
            st["think_retries"] += retries
            if failed:
                st["think_failed"] += 1

    def _log_mode_once(self) -> None:
        with self._lock:
            if self._mode_logged:
                return
            self._mode_logged = True
        logger.warning("[MT DÜŞÜNME] Düşünerek çeviri kipi: model düşünmesini kapatmıyor; her "
                       "parçaya ayrı düşünme payı verilir (taban %d + karakter başına %s "
                       "token, en çok %d), pay dolup cevap gelmezse parça bir kez yeniden "
                       "denenir", int(MT_THINK_ALLOWANCE_BASE_TOKENS),
                       format_seconds(float(MT_THINK_ALLOWANCE_PER_CHAR)), self.cap)

    def _log_seen_once(self, total: int) -> None:
        with self._lock:
            if self._seen_logged:
                return
            self._seen_logged = True
        logger.warning("[MT DÜŞÜNME] Model düşünüyor (%d token ölçüldü); yanıtlar bundan "
                       "sonra akışla okunacak ve düşünme %d token'da kesilecek",
                       total, self.budget)

    def _log_item(self, attempts: List[GenerationResult], final: GenerationResult,
                  no_answer: bool, chars: Optional[int]) -> None:
        """Kipteki parçanın tek günlük satırı (gerçek ölçüm, KARARLAR 22 c)."""
        tokens = sum(r.thinking_tokens for r in attempts)
        measured = [r.thinking_seconds for r in attempts if r.thinking_seconds is not None]
        seconds = sum(measured) if measured else None
        size = f"{chars} karakterlik parça" if chars is not None else "parça"
        budget = final.think_budget or self.allowance(chars or 0)
        if no_answer:
            what = "süresi" if is_timeout_error(final.error) else "payı"
            logger.warning("[MT DÜŞÜNME] Düşünerek çeviri: %s — düşünme %s doldu, yeniden "
                           "denemede de cevap gelmedi (düşünme %d token / %s, pay %d token, "
                           "%d deneme); parça özgün dilinde kalıyor (çevrilmedi olarak "
                           "işaretlenir, QC görür)", size, what, tokens,
                           _seconds_text(seconds), budget, len(attempts))
            return
        retry = f", {len(attempts) - 1} yeniden deneme" if len(attempts) > 1 else ""
        answer = final.output_tokens if final.output_tokens is not None else "?"
        logger.info("[MT DÜŞÜNME] Düşünerek çeviri: %s — düşünme %d token / %s (pay %d "
                    "token), yanıt %s token%s", size, tokens, _seconds_text(seconds),
                    budget, answer, retry)

    # ---------------------------------------------------- üretim
    def run(self, attempt: Callable[[bool], GenerationResult], can_harden: bool, *,
            thinking_mode: Any = False, short: bool = False, shorter: Any = None,
            chars: Optional[int] = None) -> GenerationResult:
        mode_of = thinking_mode if callable(thinking_mode) else (lambda: bool(thinking_mode))
        if shorter is None:
            shorter_of = (lambda: bool(can_harden))
        elif callable(shorter):
            shorter_of = shorter
        else:
            shorter_of = (lambda: bool(shorter))
        in_mode = bool(mode_of())
        hard = self.hard_mode         # yapışkan sert kip: kapatma işe yaradı (her kipte)
        result = attempt(hard)
        attempts = [result]
        self._observe(result)
        retries = 0
        mode_used = in_mode
        if is_no_answer(result):
            if bool(mode_of()):           # yapışkan bayrak bu denemede konmuş olabilir
                mode_used = True
                for _ in range(max(0, int(MT_THINK_RETRY_COUNT))):
                    use_shorter = not hard and bool(shorter_of())
                    logger.warning("[MT DÜŞÜNME] Düşünme payı doldu, cevap gelmedi (%d token, "
                                   "pay %d); parça bir kez %s yeniden deneniyor",
                                   result.thinking_tokens, result.think_budget or self.budget,
                                   "resmî daha kısa düşünme yoluyla" if use_shorter
                                   else "aynı istekle")
                    result = attempt(use_shorter or hard)
                    attempts.append(result)
                    self._observe(result)
                    retries += int(use_shorter)
                    if not is_no_answer(result):
                        if use_shorter and result.error is None \
                                and not result.thinking_substantive:
                            # Sert yol düşünmeyi gerçekten KAPATTI (KARARLAR 22)
                            with self._lock:
                                if not self.hard_mode:
                                    self.hard_mode = True
                                    logger.info("[MT DÜŞÜNME] Sert kapatma işe yaradı (yanıtta "
                                                "düşünce yok); koşunun kalanında doğrudan "
                                                "kullanılacak")
                        break
            elif result.thinking_exceeded and result.error:
                for _ in range(max(0, int(MT_THINK_RETRY_COUNT))):
                    if hard or not can_harden:
                        break
                    hard = True
                    retries += 1
                    logger.warning("[MT DÜŞÜNME] Düşünme sınırı aşıldı (%d > %d token); "
                                   "parça düşünme sert kapatılarak yeniden deneniyor",
                                   result.thinking_tokens, self.budget)
                    result = attempt(True)
                    attempts.append(result)
                    self._observe(result)
                    if not (result.thinking_exceeded and result.error):
                        if result.error is None:
                            with self._lock:
                                if not self.hard_mode:
                                    self.hard_mode = True
                                    logger.info("[MT DÜŞÜNME] Sert kapatma işe yaradı; koşunun "
                                                "kalanında doğrudan kullanılacak")
                        break
        no_answer = is_no_answer(result)
        if result.thinking_exceeded and result.error:
            result = dataclasses.replace(result, text="", error=MSG_THINK_BUDGET.format(
                tokens=result.thinking_tokens, budget=result.think_budget or self.budget))
        total = sum(r.thinking_tokens for r in attempts)
        if mode_used:
            self._log_mode_once()
        elif total > 0:
            self._log_seen_once(total)
        measured = [r.thinking_seconds for r in attempts if r.thinking_seconds is not None]
        result = dataclasses.replace(
            result, thinking_tokens=total,
            thinking_estimated=any(r.thinking_estimated for r in attempts),
            think_attempts_exceeded=sum(int(r.thinking_exceeded) for r in attempts),
            think_retries=retries,
            thinking_seconds=sum(measured) if measured else None)
        failed = bool((result.thinking_exceeded and result.error) or (mode_used and no_answer))
        self._record(attempts, result, retries, failed)
        if mode_used:
            self._log_item(attempts, result, no_answer, chars)
            self.note_segment(no_answer, short=short)
        return result

    def note_segment(self, no_answer: bool, short: bool = False) -> Optional[str]:
        """Kipteki parçanın sonucunu kayan pencereye işle; devre yeni açıldıysa neden.

        *no_answer*: parça pay dolup yeniden denemede de cevapsız kaldı;
        *short*: kısa parça (başlık) penceresi.
        """
        with self._lock:
            if self.tripped:
                return None
            window = self._windows[bool(short)]
            window.append(bool(no_answer))
            del window[:-max(1, int(MT_THINK_BREAKER_WINDOW))]
            samples, failures = len(window), sum(window)
            if (not failures or samples < max(1, int(MT_THINK_BREAKER_MIN_SAMPLES))
                    or failures < float(MT_THINK_BREAKER_RATIO) * samples - 1e-9):
                return None
            self.tripped = thinking_no_answer_reason(count=failures, window=samples)
            reason = self.tripped
        logger.error("[MT DÜŞÜNME] Düşünme devre kesicisi açıldı (%s penceresi, son %d "
                     "parçanın %d tanesi cevapsız): %s", "kısa parça" if short else "gövde",
                     samples, failures, reason)
        return reason


def _attach_partial(exc: BackendUnavailable,
                    results: Sequence[Optional[GenerationResult]]) -> None:
    """Toplu üretim yarıda kaldı: tamamlanan sonuçları istisnaya iliştir (F4-07).

    ``exc.partial_results`` girdi sırasıyla istek başına sonuç (denenmeyen
    öğe None). Çağıran (çevirmen) başarılı öğeleri kullanır; istisna yine
    yukarı çıkar (devre, çıkış 5 ve sıralı yedek davranışı değişmez).
    """
    done = sum(1 for r in results if r is not None)
    exc.partial_results = tuple(results)
    if done:
        logger.warning("[MT ARKA UÇ] Toplu üretim yarıda kaldı: %d/%d parçanın sonucu "
                       "istisnayla (partial_results) çağırana iletiliyor", done, len(results))


# ---------------------------------------------------------------------------
# Taban sınıf (protokol + ortak devre kesici + toplu üretim)
# ---------------------------------------------------------------------------
class MTBackend:
    """Arka uç protokolü ve ortak mekanik. Alt sınıflar ``load``,
    ``generate`` ve ``describe``'ı doldurur."""

    kind: str = ""

    def __init__(self, spec):
        self.spec = spec
        self.options: Dict[str, Any] = dict(getattr(spec, "options", None) or {})
        self._circuit_lock = threading.Lock()
        self._consecutive_failures = 0
        self._consecutive_item_errors = 0   # sunucu yanıt verdi ama parça HTTP hatası (F4-05)
        self._circuit_reason: Optional[str] = None
        self._circuit_exc: Type[BackendUnavailable] = BackendUnavailable
        self.think_budget = resolve_think_budget(spec)
        self.think_cap = resolve_think_cap(spec)
        self.think_guard = ThinkingGuard(self.think_budget, self.think_cap)
        # G2b-1 (2026-10-05): düşünme sınıfı (yüklemede ``set_profile``) ve
        # kapatma doğrulamasının yapışkan bayrağı. G2b-2: düşünerek çeviri kipi
        # bunu okur (``thinking_mode``).
        self.thinking_monitor = ThinkingOffMonitor(self._monitor_label)

    # ---------------------------------------------------------------- protokol
    def load(self) -> None:
        raise NotImplementedError

    def generate(self, engine, req: GenerationRequest) -> GenerationResult:
        """Tek öğe: ``generate_once`` denemeleri düşünme bekçisinden geçer.

        Her denemenin düşünme sınırı ve süresi ``ThinkingGuard.plan``'dan
        (``req.think_budget`` / ``req.time_limit_s`` olarak arka uca gider):
        kip dışında yoklama bütçesi ve bütün denemelerin paylaştığı
        ``request_timeout`` (eski davranış); düşünerek çeviride parçanın payı ve
        paya / ölçülen hıza göre ölçekli, denemelerin paylaştığı süre. Kipte pay
        dolup cevapsız kalan istek koşu boyunca hatırlanır (açgözlü üretimde):
        aynısı yeniden gelirse modele gidilmeden aynı öğe hatası döner.
        Devre kesici bu öğede açıldıysa ``ThinkingBudgetExceeded`` fırlar.
        """
        self.check_circuit()
        guard = self.think_guard
        cached = guard.cached_no_answer(req.instruction, req.text)
        if cached is not None:
            return cached
        if not guard.hard_mode and self._soft_off_default():
            guard.adopt_hard_mode("Qwen3 '/no_think' resmî kapatma yolu ilk denemeden "
                                  "kullanılıyor (KARARLAR 22)")
        start = time.monotonic()
        chars = len(req.text or "")

        def attempt(hard: bool) -> GenerationResult:
            mode = self.thinking_mode()
            budget, limit = guard.plan(chars, req.max_tokens, mode, self.request_timeout())
            sub = dataclasses.replace(req, think_budget=budget,
                                      time_limit_s=limit if mode else None)
            return self._observe_attempt(
                self.generate_once(engine, sub, hard=hard, deadline=start + limit), hard)

        result = guard.run(attempt, can_harden=self.can_harden(),
                           thinking_mode=self.thinking_mode, short=short_item(chars),
                           shorter=self.shorter_thinking_available, chars=chars)
        tripped = guard.tripped
        if tripped:
            self.open_circuit(tripped, ThinkingBudgetExceeded)
            raise ThinkingBudgetExceeded(tripped)
        if is_no_answer(result) and self.thinking_mode() and self.temperature() <= 0:
            # Yalnız açgözlü üretimde (sıcaklık 0, varsayılan): aynı istek aynı
            # sonucu verir. Kayıt sıcaklık verdiyse yeniden deneme farklı olabilir.
            guard.remember_no_answer(req.instruction, req.text, result)
        return result

    def generate_once(self, engine, req: GenerationRequest, *, hard: bool = False,
                      deadline: Optional[float] = None) -> GenerationResult:
        """Tek deneme (alt sınıf doldurur). ``hard``: düşünmeyi sert kapat."""
        raise NotImplementedError

    def describe(self) -> str:
        raise NotImplementedError

    # ---------------------------------------------------------------- düşünme
    def thinking_capable(self) -> bool:
        """Model düşünen model mi (sunucu yeteneği / şablon belirtisi). Alt sınıf doldurur."""
        return False

    def use_stream(self) -> bool:
        """Yanıt akış hâlinde okunsun mu: düşünen ya da düşündüğü ölçülen model."""
        return bool(self.thinking_capable() or self.think_guard.seen)

    def hard_off_available(self) -> bool:
        """Bu arka uçta resmî belgeyle doğrulanmış sert kapatma yolu var mı (alt sınıf)."""
        return supports_no_think(self.spec)

    # ---------------------------------------------- düşünme sınıfı (G2b-1)
    def _monitor_label(self) -> str:
        try:
            return self.describe()
        except Exception:   # describe alt sınıfın alanlarına dayanır; etiket bekçiyi düşürmesin
            return getattr(self.spec, "name", "") or self.kind or "arka uç"

    def thinking_profile(self) -> ThinkingProfile:
        """Yüklemede belirlenen statik düşünme sınıfı (``thinking.ThinkingProfile``)."""
        return self.thinking_monitor.profile

    def thinking_class(self) -> str:
        """Koşudaki etkin sınıf: kapatma tutmadıysa "kapatilamaz" (yapışkan)."""
        return self.thinking_monitor.effective_class

    def _log_thinking_profile(self) -> None:
        """Yükleme sonunda düşünme sınıfını günlüğe yaz (bir satır)."""
        profile = self.thinking_monitor.profile
        path = f", resmî kapatma yolu: {profile.off_path}" if profile.off_path else ""
        logger.info("[MT DÜŞÜNME] %s: düşünme sınıfı '%s'%s (%s)", self._monitor_label(),
                    self.thinking_monitor.effective_class, path,
                    profile.basis or "gerekçe yok")

    def thinking_off_requested(self, hard: bool) -> bool:
        """Bu denemede RESMÎ bir düşünme kapatma isteği gönderildi mi (alt sınıf).

        Taban: yalnız sert denemede Qwen3 karma "/no_think" yumuşak anahtarı.
        """
        return bool(hard) and wants_thinking_off(self.spec) and supports_no_think(self.spec)

    def _observe_attempt(self, result: GenerationResult, hard: bool) -> GenerationResult:
        """Deneme sonucunu kapatma doğrulamasından geçir (sonuç aynen döner)."""
        monitor = getattr(self, "thinking_monitor", None)
        if monitor is None:
            return result
        return monitor.observe(result, off_requested=self.thinking_off_requested(hard))

    def can_harden(self) -> bool:
        return wants_thinking_off(self.spec) and self.hard_off_available()

    # ------------------------------------------- düşünerek çeviri (G2b-2)
    def thinking_mode(self) -> bool:
        """Düşünerek çeviri kipi açık mı (KARARLAR 22).

        - Etkin düşünme sınıfı "kapatilamaz": keşif/yüklemede öyle ya da koşuda
          kapatma tutmadı (``thinking_monitor`` yapışkan bayrağı), ya da
        - kayıt düşünmeyi kapatmak İSTEMİYOR (``think: true`` / ``null``) ve
          model düşünüyor (yetenek bildiriyor ya da düşünmesi ölçüldü).
        Düşünmeyen modelde (HY-MT dahil) hep False: istek ve davranış değişmez.
        """
        monitor = getattr(self, "thinking_monitor", None)
        if monitor is not None and monitor.effective_class == THINKING_ALWAYS:
            return True
        return (not wants_thinking_off(self.spec)) and bool(
            self.thinking_capable() or self.think_guard.seen)

    def _soft_off_default(self) -> bool:
        """Modelin resmî kapatma yolu Qwen3 "/no_think" mi (G2b-1 sınıfı; bu yol
        yalnız sert kipte gider). KARARLAR 22 "kapatılabiliyorsa kapatılır": o
        zaman sert kip ilk denemeden açılır (llama-cpp GGUF Qwen3 karma modeli)."""
        monitor = getattr(self, "thinking_monitor", None)
        return bool(monitor is not None
                    and monitor.profile.off_path == THINKING_OFF_QWEN3_SOFT
                    and wants_thinking_off(self.spec) and supports_no_think(self.spec))

    def shorter_thinking_available(self) -> bool:
        """Kipte TEK yeniden deneme için resmî daha kısa düşünme yolu var mı
        (sert kip): taban, sert kapatma yolu (Qwen3 "/no_think", Ollama "low",
        llama.cpp server ``reasoning_effort: none``); alt sınıf genişletir."""
        return self.can_harden()

    def thinking_item_seconds(self, chars: int, out_limit: int) -> float:
        """Kipte bir parçanın (bütün denemeleri) süre sınırı (``ThinkingGuard.item_seconds``)."""
        return self.think_guard.item_seconds(chars, out_limit, self.request_timeout())

    def attempt_think_budget(self, req: GenerationRequest) -> int:
        """Bu denemenin düşünme sınırı: ``req.think_budget`` (``generate`` koyar)
        ya da arka ucun yoklama bütçesi."""
        value = getattr(req, "think_budget", None)
        return int(value) if value else int(self.think_budget)

    def attempt_time_label(self, req: GenerationRequest) -> float:
        """Zaman aşımı iletisindeki süre: kipte parçanın süre sınırı, değilse
        istek zaman aşımı."""
        value = getattr(req, "time_limit_s", None)
        return float(value) if value else self.request_timeout()

    def stream_max_tokens(self, req: GenerationRequest) -> int:
        """Akışlı (düşünen) istekte üretim sınırı: yanıt sınırı + düşünme sınırı.

        Düşünme sınırda kesildiği için yanıt payı düşünmeye yenmez. Düşünerek
        çeviride düşünme sınırı parçanın payıdır (istek token sınırı = pay +
        çeviri sınırı, KARARLAR 22 b).
        """
        return int(req.max_tokens) + self.attempt_think_budget(req)

    @staticmethod
    def remaining(deadline: Optional[float], fallback: float) -> float:
        """Paylaşılan süre sınırından kalan saniye (sınır yoksa ``fallback``)."""
        if deadline is None:
            return float(fallback)
        return deadline - time.monotonic()

    def health(self) -> Optional[str]:
        """None = sağlıklı; aksi Türkçe neden (devre açık)."""
        return self._circuit_reason

    def generate_batch(self, engine,
                       reqs: Sequence[GenerationRequest]) -> List[GenerationResult]:
        """Toplu üretim: ``len(çıktı) == len(reqs)``, sıra korunur.

        Öğe hatası ``GenerationResult.error``'dadır; arka uç tümden
        kullanılamazsa ``BackendUnavailable`` fırlar (kalan istekler
        gönderilmez). 2026-09-29 (F4-07): istisnanın ``partial_results``
        alanı kopmadan önce tamamlanan öğelerin sonuçlarını taşır (girdi
        sırasıyla; denenmeyen öğe None) — başarılı çeviriler atılmaz.
        """
        reqs = list(reqs)
        if not reqs:
            return []
        self.check_circuit()
        workers = min(self.max_parallel(), len(reqs))
        results: List[Optional[GenerationResult]] = [None] * len(reqs)
        if workers <= 1:
            for index, req in enumerate(reqs):
                try:
                    results[index] = self._generate_safe(engine, req)
                except BackendUnavailable as exc:
                    _attach_partial(exc, results)
                    raise
        else:
            stop = threading.Event()
            failure: List[BackendUnavailable] = []

            def _one(index: int) -> None:
                if stop.is_set():
                    return
                try:
                    results[index] = self._generate_safe(engine, reqs[index])
                except BackendUnavailable as exc:
                    stop.set()
                    failure.append(exc)

            with ThreadPoolExecutor(max_workers=workers,
                                    thread_name_prefix=f"mt-{self.kind or 'arka-uc'}") as pool:
                list(pool.map(_one, range(len(reqs))))
            if failure:
                _attach_partial(failure[0], results)
                raise failure[0]
        return [r if r is not None else GenerationResult(error=MSG_BAD_RESPONSE.format(
                    server=self.describe(), detail="sonuç yok"))
                for r in results]

    # ---------------------------------------------------------------- ayarlar
    def max_parallel(self) -> int:
        """Toplu üretimde eşzamanlı istek (options.max_parallel; varsayılan 1)."""
        value = _positive_int(self.options.get("max_parallel"))
        if value is None:
            value = MT_HTTP_MAX_PARALLEL
        return max(1, min(int(value), MT_HTTP_MAX_PARALLEL_LIMIT))

    def request_timeout(self) -> float:
        """Tek üretim isteğinin zaman aşımı (options.request_timeout_s ya da MT_HTTP_TIMEOUT_S)."""
        value = _positive_float(self.options.get("request_timeout_s"))
        return value if value is not None else float(MT_HTTP_TIMEOUT_S)

    def temperature(self) -> float:
        return _number(self.options.get("temperature"), MT_GREEDY_TEMPERATURE)

    def seed(self) -> int:
        value = self.options.get("seed")
        if isinstance(value, bool):
            return MT_GREEDY_SEED
        try:
            return int(value) if value is not None else MT_GREEDY_SEED
        except (TypeError, ValueError):
            return MT_GREEDY_SEED

    def repeat_penalty(self) -> float:
        return _number(self.options.get("repeat_penalty"), MT_REPEAT_PENALTY)

    # ---------------------------------------------------------------- devre
    def check_circuit(self) -> None:
        """Devre açıksa ağa gitmeden ``BackendUnavailable`` (ya da açan alt sınıf)."""
        reason = self._circuit_reason
        if reason:
            raise self._circuit_exc(reason)

    def record_success(self) -> None:
        if self._consecutive_failures or self._consecutive_item_errors:
            with self._circuit_lock:
                self._consecutive_failures = 0
                self._consecutive_item_errors = 0

    def record_item_error(self, reason: str) -> bool:
        """Sunucu yanıt verdi ama parça HTTP hatasıyla çevrilemedi (F4-05).

        Bağlantı sayacından AYRI art arda sayaç; ``MT_HTTP_SERVER_ERROR_MAX_ITEMS``
        parçada bir başarı gelmezse devre ``reason`` ile açılır (True).
        """
        with self._circuit_lock:
            self._consecutive_item_errors += 1
            limit = max(1, int(MT_HTTP_SERVER_ERROR_MAX_ITEMS))
            if self._consecutive_item_errors >= limit and not self._circuit_reason:
                self._circuit_reason = reason
                logger.error("[MT ARKA UÇ] Devre açıldı (%d parça art arda sunucu hatası): %s",
                             self._consecutive_item_errors, reason)
            return bool(self._circuit_reason)

    def record_failure(self, reason: str) -> bool:
        """Art arda başarısızlık sayacını artır; eşik aşılırsa devreyi aç (True)."""
        with self._circuit_lock:
            self._consecutive_failures += 1
            limit = max(1, int(MT_HTTP_CIRCUIT_MAX_FAILURES))
            if self._consecutive_failures >= limit and not self._circuit_reason:
                self._circuit_reason = reason
                logger.error("[MT ARKA UÇ] Devre açıldı (%d art arda hata): %s",
                             self._consecutive_failures, reason)
            return bool(self._circuit_reason)

    def open_circuit(self, reason: str,
                     exc_cls: Type[BackendUnavailable] = BackendUnavailable) -> None:
        """Devreyi hemen aç (bellek yetersizliği, model sunucuda yok, düşünme devre kesicisi)."""
        with self._circuit_lock:
            if not self._circuit_reason:
                self._circuit_reason = reason
                self._circuit_exc = exc_cls
                logger.error("[MT ARKA UÇ] Devre açıldı: %s", reason)

    # ---------------------------------------------------------------- iç
    def _generate_safe(self, engine, req: GenerationRequest) -> GenerationResult:
        """``generate``; beklenmeyen istisna öğe hatasına döner (BackendUnavailable hariç)."""
        try:
            result = self.generate(engine, req)
        except BackendUnavailable:
            raise
        except Exception as exc:   # arka uç hatası tek öğeyi düşürür, batch'i değil
            logger.warning("[MT ARKA UÇ] Beklenmeyen üretim hatası: %s: %s",
                           type(exc).__name__, exc)
            return GenerationResult(error=MSG_BAD_RESPONSE.format(
                server=self.describe(), detail=short_detail(f"{type(exc).__name__}: {exc}")))
        if not isinstance(result, GenerationResult):
            return GenerationResult(error=MSG_BAD_RESPONSE.format(
                server=self.describe(), detail="sonuç türü geçersiz"))
        return result


# ---------------------------------------------------------------------------
# Fabrika
# ---------------------------------------------------------------------------
def create_backend(spec) -> Optional[MTBackend]:
    """``spec.backend``'e göre arka uç nesnesi (ağ çağrısı YOK; o ``load()``'da).

    - ``mlx`` -> None: ``BookTranslator``'ın bugünkü MLX/transformers yolu
      AYNEN çalışır (TASARIM B.2).
    - ``ollama`` -> ``OllamaBackend``; ``openai`` -> ``OpenAIBackend``;
      ``llamacpp`` -> ``LlamaCppBackend``.
    - Bilinmeyen metin değeri -> ``BackendUnavailable(MSG_BAD_ID)``; metin
      olmayan değer (None, sahte nesne) -> None (MLX, EngineSpec varsayılanı).
    Sunucu adresi yerel değilse kurucu ``BackendUnavailable(MSG_REMOTE_REFUSED)``
    fırlatır (``http_util.check_local_url``).
    """
    backend = getattr(spec, "backend", BACKEND_MLX)
    # Metin olmayan değer (None, test iskeletlerindeki MagicMock motorun
    # kendiliğinden ürettiği öznitelik) "belirtilmemiş" sayılır: EngineSpec
    # varsayılanı gibi MLX yolu. Böylece bugünkü yol yanlışlıkla arka uca
    # yönelmez; geçersiz METİN değeri aşağıda yine reddedilir.
    if not isinstance(backend, str) or backend in (BACKEND_MLX, ""):
        return None
    if backend == BACKEND_OLLAMA:
        from .http_backends import OllamaBackend
        return OllamaBackend(spec)
    if backend == BACKEND_OPENAI:
        from .http_backends import OpenAIBackend
        return OpenAIBackend(spec)
    if backend == BACKEND_LLAMACPP:
        from .llamacpp_backend import LlamaCppBackend
        return LlamaCppBackend(spec)
    raise BackendUnavailable(MSG_BAD_ID.format(
        engine_id=getattr(spec, "name", ""),
        why=f"bilinmeyen arka uç {backend!r}"))
