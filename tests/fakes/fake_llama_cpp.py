"""Sahte ``llama_cpp`` modülü (llama-cpp-python yerine) — gerçek model YÜKLENMEZ.

``make_fake_llama_module()`` bağımsız bir modül nesnesi üretir; fixture
``fake_llama`` onu ``sys.modules["llama_cpp"]``'ye koyar. Böylece:

- ``importlib.util.find_spec("llama_cpp")`` sahte modülün ``__spec__``'ini döner
  (``__spec__`` None olsaydı ``find_spec`` ValueError verirdi);
- ``from llama_cpp import Llama`` sahte sınıfı verir.

Sahte ``Llama``:

- kurucu argümanlarını kaydeder (``mod.instances[i].kwargs``); ``model_path``
  yoksa gerçek kütüphane gibi ``ValueError("Model path does not exist: …")``;
  ``mod.config.load_error`` doluysa ``ValueError(load_error)`` (desteklenmeyen
  mimari benzetimi);
- ``create_chat_completion`` / ``create_completion`` OpenAI biçimli sözlük
  döner; üretim "token token" (sözcük sözcük) benzetilir: her adımda
  ``stopping_criteria(input_ids, logits)`` çağrılır (True -> durur,
  ``finish_reason "stop"``), ``max_tokens`` aşılırsa ``"length"``, ``stop``
  dizgilerinde kesilir; ``config.token_delay_s`` adım başına bekler.
- her çağrı ``mod.calls``'a ``(yöntem, kwargs)`` olarak eklenir.
- ``create_chat_completion(stream=True)`` gerçek kütüphane gibi parça üreteci
  döner (ilk parça ``delta.role``, sonra ``delta.content`` parçaları, son
  parçada ``finish_reason``); her akış ``mod.streams``'e
  ``{"yielded", "total", "closed_early"}`` olarak kaydedilir (üreteç erken
  kapatılınca ``closed_early`` True — "döngüden çıkıldı" kanıtı).

Varsayılan yanıt sahte sunucuyla aynı: sohbette son user içeriğinin ilk
boş satırdan sonrası, ham prompt'ta ``<|extra_4|>`` … ``<|extra_0|>`` arası
-> ``"ÇEVRİLDİ: " + metin``.
"""

from __future__ import annotations

import importlib.machinery
import inspect
import os
import time
import types
from typing import Any, Callable, Dict, List, Optional, Sequence

from .fake_llm_server import (
    THINK_BLOCK,
    default_reply,
    extract_chat_text,
    extract_prompt_text,
    stream_pieces,
)

__all__ = ["FAKE_LLAMA_VERSION", "make_fake_llama_module"]

FAKE_LLAMA_VERSION = "0.3.35-sahte"


def make_fake_llama_module(reply: Optional[Callable[..., str]] = None, *,
                           think_block: bool = False, prefix: str = "",
                           finish_reason: str = "stop", token_delay_s: float = 0.0,
                           load_error: Optional[str] = None,
                           chat_template: Optional[str] = None,
                           architecture: str = "qwen3") -> types.ModuleType:
    """Yeni bir sahte ``llama_cpp`` modülü üret (ayarlar ``mod.config``'te, değiştirilebilir)."""
    mod = types.ModuleType("llama_cpp")
    mod.__spec__ = importlib.machinery.ModuleSpec("llama_cpp", None)
    mod.__version__ = FAKE_LLAMA_VERSION
    mod.__file__ = "<sahte llama_cpp>"
    mod.config = types.SimpleNamespace(
        reply=reply or default_reply,   # reply(text) ya da reply(text, kwargs) -> str
        think_block=think_block,
        prefix=prefix,
        finish_reason=finish_reason,
        token_delay_s=token_delay_s,
        load_error=load_error,
        chat_template=chat_template,
        architecture=architecture,
    )
    mod.instances = []   # oluşturulan Llama nesneleri
    mod.calls = []       # (yöntem adı, kwargs) — tüm nesneler
    mod.streams = []     # akışlı çağrıların kaydı

    class StoppingCriteriaList(list):
        """Gerçekteki gibi: ölçütlerden biri True dönerse üretim durur."""

        def __call__(self, input_ids, logits) -> bool:
            return any(criterion(input_ids, logits) for criterion in self)

    class LogitsProcessorList(list):
        def __call__(self, input_ids, scores):
            for processor in self:
                scores = processor(input_ids, scores)
            return scores

    class Llama:
        def __init__(self, model_path: str, *, n_ctx: int = 512, n_gpu_layers: int = 0,
                     seed: int = 4294967295, verbose: bool = True, **kwargs: Any):
            self.kwargs: Dict[str, Any] = dict(model_path=model_path, n_ctx=n_ctx,
                                               n_gpu_layers=n_gpu_layers, seed=seed,
                                               verbose=verbose, **kwargs)
            mod.instances.append(self)
            cfg = mod.config
            if cfg.load_error:
                raise ValueError(cfg.load_error)
            if not os.path.exists(model_path):
                raise ValueError(f"Model path does not exist: {model_path}")
            self.model_path = model_path
            self._n_ctx = n_ctx
            self.metadata: Dict[str, str] = {"general.architecture": cfg.architecture}
            if cfg.chat_template is not None:
                self.metadata["tokenizer.chat_template"] = cfg.chat_template
            self.calls: List[tuple] = []
            self.closed = False

        # ---------------------------------------------------------- yardımcı
        def n_ctx(self) -> int:
            return self._n_ctx

        def tokenize(self, text: bytes, add_bos: bool = True, special: bool = False) -> List[int]:
            words = (text.decode("utf-8", "replace") if isinstance(text, bytes) else text).split()
            return ([1] if add_bos else []) + list(range(2, 2 + len(words)))

        def close(self) -> None:
            self.closed = True

        def _record(self, name: str, kwargs: Dict[str, Any]) -> None:
            self.calls.append((name, kwargs))
            mod.calls.append((name, kwargs))

        def _reply(self, text: str, kwargs: Dict[str, Any]) -> str:
            fn = mod.config.reply
            try:
                nparams = len(inspect.signature(fn).parameters)
            except (TypeError, ValueError):
                nparams = 1
            out = fn(text, kwargs) if nparams >= 2 else fn(text)
            out = mod.config.prefix + out
            if mod.config.think_block:
                out = THINK_BLOCK + out
            return out

        def _generate(self, content: str, max_tokens: Optional[int],
                      stop: Optional[Sequence[str]], stopping_criteria) -> tuple:
            """Sözcük sözcük üretim benzetimi -> (metin, finish_reason, token sayısı)."""
            finish = mod.config.finish_reason
            if stop:
                for s in ([stop] if isinstance(stop, str) else stop):
                    if s and s in content:
                        content = content.split(s, 1)[0]
                        finish = "stop"
            tokens = content.split(" ")
            produced: List[str] = []
            input_ids: List[int] = []
            for i, tok in enumerate(tokens):
                if max_tokens is not None and max_tokens > 0 and len(produced) >= max_tokens:
                    finish = "length"
                    break
                if mod.config.token_delay_s:
                    time.sleep(mod.config.token_delay_s)
                produced.append(tok)
                input_ids.append(i + 2)
                if stopping_criteria is not None and stopping_criteria(list(input_ids), []):
                    finish = "stop"
                    break
            return " ".join(produced), finish, len(produced)

        def _stream_chat(self, content: str, max_tokens: Optional[int]):
            """``stream=True``: parça üreteci (her içerik parçası bir token)."""
            pieces = stream_pieces(content)
            state = {"yielded": 0, "total": len(pieces), "closed_early": False}
            mod.streams.append(state)
            cid = f"chatcmpl-sahte-{len(mod.calls)}"

            def chunk(delta: Dict[str, Any], finish: Optional[str]) -> Dict[str, Any]:
                return {"id": cid, "object": "chat.completion.chunk", "created": 0,
                        "model": self.model_path,
                        "choices": [{"index": 0, "delta": delta, "logprobs": None,
                                     "finish_reason": finish}]}

            def gen():
                finished = False
                try:
                    yield chunk({"role": "assistant"}, None)
                    finish = mod.config.finish_reason
                    for piece in pieces:
                        if max_tokens is not None and max_tokens > 0 \
                                and state["yielded"] >= max_tokens:
                            finish = "length"
                            break
                        if mod.config.token_delay_s:
                            time.sleep(mod.config.token_delay_s)
                        state["yielded"] += 1
                        yield chunk({"content": piece}, None)
                    finished = True
                    yield chunk({}, finish)
                finally:
                    if not finished:
                        state["closed_early"] = True

            return gen()

        # ---------------------------------------------------------- üretim
        def create_chat_completion(self, messages: List[Dict[str, Any]], *,
                                   max_tokens: Optional[int] = None,
                                   temperature: float = 0.2, top_p: float = 0.95,
                                   repeat_penalty: float = 1.0,
                                   stop: Optional[Sequence[str]] = None,
                                   seed: Optional[int] = None,
                                   stopping_criteria=None, **kwargs: Any) -> Dict[str, Any]:
            call = dict(messages=messages, max_tokens=max_tokens, temperature=temperature,
                        top_p=top_p, repeat_penalty=repeat_penalty, stop=stop, seed=seed,
                        stopping_criteria=stopping_criteria, **kwargs)
            self._record("create_chat_completion", call)
            text = extract_chat_text(messages)
            if kwargs.get("stream"):
                return self._stream_chat(self._reply(text, call), max_tokens)
            content, finish, n = self._generate(self._reply(text, call), max_tokens,
                                                stop, stopping_criteria)
            return {
                "id": f"chatcmpl-sahte-{len(mod.calls)}", "object": "chat.completion",
                "created": 0, "model": self.model_path,
                "choices": [{"index": 0,
                             "message": {"role": "assistant", "content": content},
                             "logprobs": None, "finish_reason": finish}],
                "usage": {"prompt_tokens": len(text.split()), "completion_tokens": n,
                          "total_tokens": len(text.split()) + n},
            }

        def create_completion(self, prompt: str, *, max_tokens: Optional[int] = 16,
                              temperature: float = 0.8, top_p: float = 0.95,
                              repeat_penalty: float = 1.0,
                              stop: Optional[Sequence[str]] = None,
                              seed: Optional[int] = None,
                              stopping_criteria=None, **kwargs: Any) -> Dict[str, Any]:
            call = dict(prompt=prompt, max_tokens=max_tokens, temperature=temperature,
                        top_p=top_p, repeat_penalty=repeat_penalty, stop=stop, seed=seed,
                        stopping_criteria=stopping_criteria, **kwargs)
            self._record("create_completion", call)
            text = extract_prompt_text(prompt)
            content, finish, n = self._generate(self._reply(text, call), max_tokens,
                                                stop, stopping_criteria)
            return {
                "id": f"cmpl-sahte-{len(mod.calls)}", "object": "text_completion",
                "created": 0, "model": self.model_path,
                "choices": [{"index": 0, "text": content, "logprobs": None,
                             "finish_reason": finish}],
                "usage": {"prompt_tokens": len(text.split()), "completion_tokens": n,
                          "total_tokens": len(text.split()) + n},
            }

        def __call__(self, prompt: str, **kwargs: Any) -> Dict[str, Any]:
            return self.create_completion(prompt, **kwargs)

    def llama_supports_gpu_offload() -> bool:
        return True

    mod.Llama = Llama
    mod.StoppingCriteriaList = StoppingCriteriaList
    mod.LogitsProcessorList = LogitsProcessorList
    mod.llama_supports_gpu_offload = llama_supports_gpu_offload
    return mod
