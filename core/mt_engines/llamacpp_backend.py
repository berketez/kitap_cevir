"""llama-cpp-python arka ucu: ``.gguf`` dosyasını süreç içinde doğrudan açar (TASARIM B.7).

llama-cpp-python İSTEĞE BAĞLI bağımlılıktır (KARARLAR 3): kurulu değilse
uygulama açılır, yalnız bu arka uç ``MSG_LLAMA_MISSING`` (+ kurulum komutu)
ile kullanılamaz. Modül yalnız ``importlib.util.find_spec`` ile aranır ve
yalnız ``load()`` içinde içe aktarılır (testte ``sys.modules`` ile değiştirilir).

- ``load()``: ``Llama(model_path, n_ctx, n_gpu_layers=-1, seed=0, verbose=False)``.
  Yükleme ana iş parçacığında ve zaman aşımı penceresinin dışında yapılır
  (MLX yüklemesiyle aynı; R3).
- Biçim ``chat`` -> ``create_chat_completion`` (şablon GGUF üst verisindeki
  ``tokenizer.chat_template``'ten gelir); biçim ``hymt`` -> ham
  ``create_completion`` (deneysel; HY-MT özel token'ları durdurucu).
- Süre sınırı: yöntem ``stopping_criteria`` kabul ediyorsa ölçüt
  ``time.monotonic() > son_an`` olduğunda üretimi durdurur ve öğe
  ``MSG_TIMEOUT`` alır — zombi üretim sınırlanır. Kurulu 0.3.35'te
  ``create_chat_completion`` bu parametreyi KABUL ETMİYOR (kaynak okundu,
  25 Eyl 2026); o yolda yalnız latex_pipeline'ın iş parçacığı zaman aşımı
  kalır (TASARIM B.7'deki yedek).
- Toplu üretim sıralıdır: ``Llama`` nesnesi iş parçacığı güvenli değildir.
- Düşünme (2026-09-28, KARARLAR 16): GGUF sohbet şablonu düşünme belirtisi
  taşıyorsa (``<think>``, ``enable_thinking``, harmony) ya da bir yanıtta
  düşünme ölçüldüyse sohbet yanıtı ``stream=True`` ile okunur: düşünme bütçeyi
  aşınca ya da süre dolunca döngüden çıkılır ve üreteç kapatılır (kurulu
  0.3.35 ``create_chat_completion`` düşünmeyi kapatacak şablon değişkeni
  almadığı için bütçe tek frendir). Düşünmeyen modelde çağrı değişmez.
"""

from __future__ import annotations

import importlib
import importlib.util
import inspect
import logging
import os
import re
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from config.defaults import MT_CONTEXT_TOKENS

from . import backends as _b
from .contracts import (
    BACKEND_LABELS,
    BACKEND_LLAMACPP,
    FORMAT_HYMT,
    LLAMA_CPP_INSTALL_CMD,
    MSG_BAD_ID,
    MSG_CONTEXT_EXCEEDED,
    MSG_EMPTY_OUTPUT,
    MSG_GGUF_LOAD_FAILED,
    MSG_GGUF_MISSING,
    MSG_LLAMA_MISSING,
    MSG_THINK_BUDGET,
    MSG_TIMEOUT,
    THINKING_NONE,
    BackendUnavailable,
    GenerationRequest,
    GenerationResult,
)
from .thinking import ThinkingProfile, template_profile

logger = logging.getLogger(__name__)

__all__ = ["LlamaCppBackend", "llama_cpp_available", "LLAMA_MODULE"]

LLAMA_MODULE = "llama_cpp"
# llama-cpp-python bağlam aşımı iletisi: "Requested tokens (N) exceed context window of M"
_CONTEXT_EXCEEDED_RE = re.compile(
    r"requested\s+tokens\s*\((\d+)\)\s*exceed\s+context\s+window\s+of\s+(\d+)",
    re.IGNORECASE)
# Tam GPU yükü (-1 = tüm katmanlar); options.n_gpu_layers ile değişir (0 = yalnız CPU).
_ALL_GPU_LAYERS = -1
# Greedy üretimde çekirdek örnekleme kapalı
_TOP_P_GREEDY = 1.0


def llama_cpp_available() -> bool:
    """llama-cpp-python bulunuyor mu (içe AKTARMADAN)."""
    try:
        return importlib.util.find_spec(LLAMA_MODULE) is not None
    except (ValueError, ImportError):
        # sys.modules'ta __spec__'siz bir nesne varsa find_spec ValueError verir
        return sys.modules.get(LLAMA_MODULE) is not None


def _accepts_param(fn: Callable, name: str) -> bool:
    """Yöntem ``name`` adlı parametreyi AÇIKÇA kabul ediyor mu (**kwargs sayılmaz)."""
    try:
        return name in inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return False


class LlamaCppBackend(_b.MTBackend):
    """``gguf:`` kimlikleri ve ``backend: "llamacpp"`` kayıtları."""

    kind = BACKEND_LLAMACPP

    def __init__(self, spec):
        super().__init__(spec)
        self.path = str(getattr(spec, "path", "") or "")
        if not self.path:
            raise BackendUnavailable(MSG_BAD_ID.format(
                engine_id=getattr(spec, "name", ""), why="GGUF dosya yolu yok"))
        self.raw = getattr(spec, "format", "") == FORMAT_HYMT
        self.n_ctx = _b._positive_int(self.options.get("n_ctx")) or int(MT_CONTEXT_TOKENS)
        self._llm: Any = None
        self._stopping_cls: Any = None
        self._deadline_ok: Dict[str, bool] = {"chat": False, "raw": False}
        self._llm_lock = threading.Lock()
        self._template_thinking = False   # GGUF sohbet şablonunda düşünme belirtisi
        # Şablon düşünmeyi üretim isteminde açıyor (ör. DeepSeek-R1 damıtması,
        # Qwen3 *-Thinking-2507): yanıt </think>'e kadar düşünmedir (VTD 2026-09-28).
        self._template_forced_open = False

    # ---------------------------------------------------------------- bilgi
    def describe(self) -> str:
        return f"{BACKEND_LABELS[BACKEND_LLAMACPP]} · {Path(self.path).name} · {self.path}"

    def max_parallel(self) -> int:
        return 1   # Llama nesnesi iş parçacığı güvenli değil

    def n_gpu_layers(self) -> int:
        value = self.options.get("n_gpu_layers")
        if value is None or isinstance(value, bool):
            return _ALL_GPU_LAYERS
        try:
            return int(value)
        except (TypeError, ValueError):
            return _ALL_GPU_LAYERS

    # ---------------------------------------------------------------- yükleme
    def load(self) -> None:
        self.check_circuit()
        if not llama_cpp_available():
            raise BackendUnavailable(MSG_LLAMA_MISSING, hint=LLAMA_CPP_INSTALL_CMD)
        if not os.path.isfile(self.path):
            raise BackendUnavailable(MSG_GGUF_MISSING.format(path=self.path))
        try:
            module = importlib.import_module(LLAMA_MODULE)
            llama_cls = getattr(module, "Llama")
        except Exception:   # kırık kurulum (yerel kütüphane yüklenemedi vb.)
            raise BackendUnavailable(MSG_LLAMA_MISSING, hint=LLAMA_CPP_INSTALL_CMD) from None
        try:
            self._llm = llama_cls(
                model_path=self.path,
                n_ctx=self.n_ctx,
                n_gpu_layers=self.n_gpu_layers(),
                seed=self.seed(),
                verbose=False,
            )
        except Exception as exc:
            raise BackendUnavailable(MSG_GGUF_LOAD_FAILED.format(
                why=_b.short_detail(f"{type(exc).__name__}: {exc}"), path=self.path)) from None
        try:
            actual = _b._positive_int(self._llm.n_ctx())
        except Exception:
            actual = None
        if actual:
            self.n_ctx = actual
        self._stopping_cls = getattr(module, "StoppingCriteriaList", None)
        self._deadline_ok = {
            "chat": _accepts_param(self._llm.create_chat_completion, "stopping_criteria"),
            "raw": _accepts_param(self._llm.create_completion, "stopping_criteria"),
        }
        metadata = getattr(self._llm, "metadata", None)
        template = metadata.get("tokenizer.chat_template") if isinstance(metadata, dict) else None
        self._template_thinking = (not self.raw) and _b.template_signals_thinking(template)
        self._template_forced_open = self._template_thinking and \
            _b.template_opens_thinking(template)
        # G2b-1: düşünme sınıfı (keşifle aynı kural; HY-MT ham biçimi düşünmez).
        self.thinking_monitor.set_profile(
            ThinkingProfile(THINKING_NONE, basis="HY-MT ham biçimi") if self.raw
            else template_profile(template, backend=BACKEND_LLAMACPP,
                                  soft_switch=_b.supports_no_think(self.spec)))
        mode = "raw" if self.raw else "chat"
        if not self._deadline_ok[mode]:
            logger.info("[MT ARKA UÇ] Kurulu llama-cpp-python bu yolda süre sınırı "
                        "(stopping_criteria) almıyor; yalnız iş parçacığı zaman aşımı geçerli")
        logger.info("[MT ARKA UÇ] %s hazır (n_ctx=%d, n_gpu_layers=%d)",
                    self.describe(), self.n_ctx, self.n_gpu_layers())
        self._log_thinking_profile()

    # ---------------------------------------------------------------- düşünme
    def thinking_capable(self) -> bool:
        return bool(self._template_thinking)

    # ---------------------------------------------------------------- üretim
    def generate_once(self, engine, req: GenerationRequest, *, hard: bool = False,
                      deadline: Optional[float] = None) -> GenerationResult:
        self.check_circuit()
        if self._llm is None:
            raise BackendUnavailable(MSG_GGUF_LOAD_FAILED.format(
                why="model yüklenmedi", path=self.path))
        timeout = self.request_timeout()
        if deadline is None:
            deadline = time.monotonic() + timeout
        # G2b-2: denemenin düşünme sınırı (kipte parçanın payı) ve zaman aşımı
        # iletisindeki süre (kipte parçanın süre sınırı; değilse istek zaman aşımı).
        budget = self.attempt_think_budget(req)
        label = self.attempt_time_label(req)
        state = {"timed_out": False}

        def _past_deadline(input_ids, logits) -> bool:
            if time.monotonic() > deadline:
                state["timed_out"] = True
                return True
            return False

        kwargs: Dict[str, Any] = {
            "max_tokens": int(req.max_tokens),
            "temperature": self.temperature(),
            "top_p": _TOP_P_GREEDY,
            "repeat_penalty": self.repeat_penalty(),
            "seed": self.seed(),
        }
        if self.raw:
            mode = "raw"
            prompt = engine.build_raw_prompt(req.instruction, req.text)
            prompt_chars = len(prompt)
            stop: List[str] = [t for t in engine.input_strip_tokens() if t]
            if stop:
                kwargs["stop"] = stop

            def _call(kw):
                return self._llm.create_completion(prompt=prompt, **kw)
        else:
            mode = "chat"
            messages = engine.build_chat_messages(req.instruction, req.text)
            if hard and _b.supports_no_think(self.spec):
                messages = _b.with_no_think(messages)
            prompt_chars = sum(len(m.get("content") or "") for m in messages)

            def _call(kw):
                return self._llm.create_chat_completion(messages=messages, **kw)

        stream = mode == "chat" and self.use_stream()
        if stream:
            kwargs["max_tokens"] = self.stream_max_tokens(req)
            kwargs["stream"] = True
        # F4-03: sınır bağlamda kalan yere kısılır; istem sığmıyorsa istek yok.
        requested = int(kwargs["max_tokens"])
        limit, early = _b.context_budget(prompt_chars, requested, self.n_ctx)
        if early:
            return GenerationResult(error=early)
        kwargs["max_tokens"] = limit
        if self._deadline_ok.get(mode) and self._stopping_cls is not None:
            kwargs["stopping_criteria"] = self._stopping_cls([_past_deadline])

        meter = _b.ThinkingMeter(budget, req.text,
                                 forced_open=stream and self._template_forced_open)
        try:
            with self._llm_lock:
                out = _call(kwargs)
                if stream:
                    out = self._consume_stream(out, meter, deadline, state)
        except Exception as exc:
            text = f"{exc}"
            m = _CONTEXT_EXCEEDED_RE.search(text)
            if m:
                return GenerationResult(error=MSG_CONTEXT_EXCEEDED.format(
                    need=int(m.group(1)), ctx=int(m.group(2))))
            message = _b.MSG_GENERATE_FAILED.format(
                server=BACKEND_LABELS[BACKEND_LLAMACPP],
                detail=_b.short_detail(f"{type(exc).__name__}: {text}"))
            if self.record_failure(message):
                raise BackendUnavailable(self.health() or message) from None
            return GenerationResult(error=message)

        self.record_success()
        if stream:
            result = self._stream_result(meter, state, label)
        elif state["timed_out"]:
            return GenerationResult(error=MSG_TIMEOUT.format(
                seconds=_b.format_seconds(label)))
        else:
            result = self._parse(out, mode, req.text, budget)
        return _b.context_overflow(result, limit, requested, prompt_chars, self.n_ctx)

    def _consume_stream(self, chunks: Any, meter: "_b.ThinkingMeter", deadline: float,
                        state: Dict[str, Any]) -> None:
        """``stream=True`` parçalarını ölçerek oku; bütçe/süre aşılınca üreteci kapat."""
        try:
            for chunk in chunks:
                choices = chunk.get("choices") if isinstance(chunk, dict) else None
                choice = choices[0] if isinstance(choices, list) and choices \
                    and isinstance(choices[0], dict) else {}
                delta = choice.get("delta") if isinstance(choice.get("delta"), dict) else {}
                reasoning = delta.get("reasoning_content")
                meter.add_thinking(reasoning if isinstance(reasoning, str) else None)
                content = delta.get("content")
                meter.add_content(content if isinstance(content, str) else None)
                if choice.get("finish_reason"):
                    reason = choice.get("finish_reason")
                    state["finish"] = "length" if reason == "length" else "stop"
                    state["done"] = True
                if meter.exceeded:
                    break
                if time.monotonic() > deadline:
                    state["timed_out"] = True
                    break
        finally:
            close = getattr(chunks, "close", None)
            if callable(close):
                close()
        return None

    def _stream_result(self, meter: "_b.ThinkingMeter", state: Dict[str, Any],
                       timeout: float) -> GenerationResult:
        fields = meter.result_fields()
        if meter.exceeded:
            logger.warning("[MT DÜŞÜNME] llama.cpp: düşünme %d token'da kesildi (sınır %d)",
                           meter.tokens, meter.budget)
            return GenerationResult(error=MSG_THINK_BUDGET.format(
                tokens=meter.tokens, budget=meter.budget), **fields)
        if state.get("timed_out"):
            return GenerationResult(error=MSG_TIMEOUT.format(
                seconds=_b.format_seconds(timeout)), **fields)
        finish = state.get("finish")
        if not meter.content.strip() or not meter.answer_text().strip():
            return GenerationResult(text="", finish_reason=finish, error=MSG_EMPTY_OUTPUT,
                                    output_tokens=meter.answer_tokens, **fields)
        return GenerationResult(text=meter.content, finish_reason=finish,
                                output_tokens=meter.answer_tokens, **fields)

    def _parse(self, out: Any, mode: str, source: str = "",
               budget: Optional[int] = None) -> GenerationResult:
        choices = out.get("choices") if isinstance(out, dict) else None
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
            return GenerationResult(error=_b.MSG_BAD_RESPONSE.format(
                server=BACKEND_LABELS[BACKEND_LLAMACPP], detail="'choices' alanı yok"))
        first = choices[0]
        reason = first.get("finish_reason")
        finish = None if reason in (None, "") else ("length" if reason == "length" else "stop")
        if mode == "raw":
            content = first.get("text")
        else:
            message = first.get("message")
            content = message.get("content") if isinstance(message, dict) else None
        usage = out.get("usage") if isinstance(out.get("usage"), dict) else {}
        output_tokens = _b._positive_int(usage.get("completion_tokens"))
        meter = _b.ThinkingMeter(budget or self.think_budget, source)
        meter.add_whole(None, content if isinstance(content, str) else "")
        fields = meter.result_fields()
        if meter.tokens:
            output_tokens = None   # sayaç düşünmeyi de içerir; yanıt payı bilinmiyor
        if not isinstance(content, str) or not content.strip() \
                or not meter.answer_text().strip():
            error = (MSG_THINK_BUDGET.format(tokens=meter.tokens, budget=meter.budget)
                     if meter.exceeded else MSG_EMPTY_OUTPUT)
            return GenerationResult(text="", finish_reason=finish, error=error,
                                    output_tokens=output_tokens, **fields)
        return GenerationResult(text=content, finish_reason=finish,
                                output_tokens=output_tokens, **fields)
