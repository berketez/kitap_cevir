"""Düşünme sınıflandırması ve kapatma doğrulaması (G2b-1, 2026-10-05).

Berke'nin 5 Ekim 2026 kararı (KARARLAR 16'yı kısmen değiştirir): "Düşünme
kapatılabiliyorsa kapatılır; kapatılamıyorsa model DÜŞÜNEREK çevirecek."
Bu modül o kararın zeminidir: modelin düşünme sınıfı, kullanılan RESMÎ
kapatma yolu ve koşu sırasında kapatmanın gerçekten tutup tutmadığının
yapışkan izlenmesi. "Düşünerek çeviri" kipinin kendisi (düşünme payı, token
sınırı, zaman aşımı, yeniden deneme, devre kesicinin yeni anlamı) G2b-2'nin
işidir; bu modül üretim davranışını DEĞİŞTİRMEZ.

Hafif modüldür: yalnız stdlib + ``chat`` + ``contracts`` (``backends`` yalnız
``qwen3_soft_switch`` içinde, çağrı anında içe aktarılır — döngü olmasın).

Sınıflar (``contracts.THINKING_CLASSES``)
-----------------------------------------
- ``yok``: düşünmeyen model (kaynak düşünme bildirmiyor, şablonda iz yok;
  HY-MT biçimi).
- ``kapatilabilir``: düşünen model ve resmî belgeli bir kapatma yolu var
  (``off_path``: ``contracts.THINKING_OFF_*``).
- ``kapatilamaz``: düşünen model, resmî kapatma yolu yok; düşünerek çevirir.
- ``bilinmiyor``: kaynak yeterli bilgi vermiyor (genel OpenAI uyumlu sunucu,
  LM Studio'nun eski /api/v0 REST'i, şablonu okunamayan model, Ollama'nın
  düşünme üst verisi vermeyen sürümü).

Resmî kaynaklar (okunma tarihi 2026-10-05)
------------------------------------------
- LM Studio ``GET /api/v1/models``: ``capabilities.reasoning`` isteğe bağlı
  nesne ``{allowed_options: ("off"|"on"|"low"|"medium"|"high")[], default}``;
  yüklü örnek ``loaded_instances[].config.context_length``
  (https://lmstudio.ai/docs/developer/rest/list).
- LM Studio ``POST /api/v1/chat``: ``reasoning`` = "off" | "low" | "medium" |
  "high" | "on" ("Will error if the model being used does not support the
  reasoning setting"); ``store``, ``max_output_tokens``, ``system_prompt``,
  ``input``; yanıt ``output[]`` (``type`` "message" / "reasoning"), ``stats``
  (https://lmstudio.ai/docs/developer/rest/chat); akış olayları
  ``reasoning.delta`` / ``message.delta`` / ``error`` / ``chat.end``
  (https://lmstudio.ai/docs/developer/rest/streaming-events). LM Studio'nun
  OpenAI uyumlu ``/v1/chat/completions`` belgesinde düşünme alanı YOK
  (https://lmstudio.ai/docs/developer/openai-compat/chat-completions): orada
  kapatma doğrulanamadı, kullanılmaz.
- Ollama ``POST /api/show``: üst düzey ``thinking = {values, default}``;
  "``values: [false]`` means the model does not support thinking"; istekte
  ``think: false`` "request no thinking output, if the model permits it"
  (https://docs.ollama.com/capabilities/thinking).
- Qwen3 karma modeller: ``enable_thinking`` sert anahtarı ve "/no_think"
  yumuşak anahtarı (https://huggingface.co/Qwen/Qwen3-8B). Qwen3.5: "does not
  officially support the soft switch of Qwen3, i.e., /think and /nothink";
  kapatma ``enable_thinking: False`` ile
  (https://huggingface.co/Qwen/Qwen3.5-35B-A3B). ``backends._QWEN3_HYBRID_RE``
  qwen3.x'i bu yüzden dışlar (belgeyle uyumlu).
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any, Callable, Iterable, Optional, Tuple

from .chat import HARMONY_MARK
from .contracts import (
    BACKEND_MLX,
    FORMAT_HYMT,
    THINKING_ALWAYS,
    THINKING_CAN_DISABLE,
    THINKING_NONE,
    THINKING_OFF_LMSTUDIO_REST,
    THINKING_OFF_OLLAMA_THINK,
    THINKING_OFF_QWEN3_SOFT,
    THINKING_OFF_TEMPLATE_SWITCH,
    THINKING_UNKNOWN,
    GenerationResult,
)

logger = logging.getLogger(__name__)

__all__ = [
    "LMSTUDIO_REASONING_OFF",
    "LMSTUDIO_REASONING_LOW",
    "ThinkingProfile",
    "ThinkingOffMonitor",
    "lmstudio_reasoning_options",
    "lmstudio_profile",
    "ollama_profile",
    "template_profile",
    "qwen3_soft_switch",
    "profile_of",
    "template_text",
]

# LM Studio REST ``reasoning`` değeri (belgeli API değişmezi, ayar değil).
LMSTUDIO_REASONING_OFF = "off"
# G2b-2 (2026-10-05, KARARLAR 22): düşünerek çeviride pay dolup cevap
# gelmeyen parçanın TEK yeniden denemesi, model izin veriyorsa
# (``allowed_options``) resmî daha kısa düşünme düzeyiyle yapılır
# (https://lmstudio.ai/docs/developer/rest/chat: "off" | "low" | "medium" |
# "high" | "on"). Belgeli API değişmezi, ayar değil.
LMSTUDIO_REASONING_LOW = "low"

# Sohbet şablonunda düşünme izleri: anahtar (enable_thinking) ve düşünme
# bloğu / harmony kanalı (backends._TEMPLATE_THINK_MARKERS ile aynı aile).
_SWITCH_MARK = "enable_thinking"
_THINK_MARKS = ("<think>", HARMONY_MARK)


@dataclass(frozen=True)
class ThinkingProfile:
    """Bir modelin düşünme sınıfı + resmî kapatma yolu + kısa Türkçe gerekçe."""
    cls: str = THINKING_UNKNOWN
    off_path: Optional[str] = None   # contracts.THINKING_OFF_*; yalnız "kapatilabilir"
    basis: str = ""                  # günlük/rapor için gerekçe (kullanıcı metni değil)


_UNKNOWN = ThinkingProfile(THINKING_UNKNOWN, basis="kaynak düşünme bilgisi vermedi")


# ---------------------------------------------------------------------------
# LM Studio
# ---------------------------------------------------------------------------
def lmstudio_reasoning_options(reasoning: Any) -> Optional[Tuple[str, ...]]:
    """``capabilities.reasoning.allowed_options`` (küçük harf); okunamazsa None."""
    if not isinstance(reasoning, dict):
        return None
    options = reasoning.get("allowed_options")
    if not isinstance(options, list):
        return None
    return tuple(str(item).strip().lower() for item in options if isinstance(item, str))


def lmstudio_profile(caps: Any) -> ThinkingProfile:
    """LM Studio REST v1 ``capabilities`` nesnesinden sınıf.

    Nesne yoksa (REST v1 yok, /api/v0 dizi döndürür) "bilinmiyor";
    ``reasoning`` yoksa "yok" (LM Studio modeli düşünen saymıyor); izinli
    seçeneklerde "off" varsa "kapatilabilir" (yerel REST ``reasoning: "off"``),
    yoksa "kapatilamaz".
    """
    if not isinstance(caps, dict):
        return ThinkingProfile(THINKING_UNKNOWN, basis="LM Studio REST v1 yetenek nesnesi yok")
    reasoning = caps.get("reasoning")
    if reasoning is None:
        return ThinkingProfile(THINKING_NONE, basis="LM Studio: capabilities.reasoning yok")
    options = lmstudio_reasoning_options(reasoning)
    if options is None:
        return ThinkingProfile(THINKING_UNKNOWN,
                               basis="LM Studio: reasoning.allowed_options okunamadı")
    if LMSTUDIO_REASONING_OFF in options:
        return ThinkingProfile(THINKING_CAN_DISABLE, THINKING_OFF_LMSTUDIO_REST,
                               basis=f"LM Studio: allowed_options {list(options)}")
    return ThinkingProfile(THINKING_ALWAYS,
                           basis=f"LM Studio: allowed_options {list(options)} 'off' içermiyor")


# ---------------------------------------------------------------------------
# Ollama
# ---------------------------------------------------------------------------
def ollama_profile(show: Any) -> ThinkingProfile:
    """Ollama ``/api/show`` yanıtından sınıf.

    Öncelik belgedeki üst veri: ``thinking.values`` yalnız ``false`` ise
    "yok"; ``false`` içeriyorsa "kapatilabilir" (``think: false``); içermiyorsa
    (gpt-oss: low/medium/high) "kapatilamaz". Üst veri yoksa: yetenek listesinde
    "thinking" varsa "bilinmiyor" (``think: false`` yine gönderilir; tuttuğu
    koşuda ölçülür), yoksa şablona bakılır.
    """
    if not isinstance(show, dict):
        return ThinkingProfile(THINKING_UNKNOWN, basis="Ollama /api/show yanıtı yok")
    meta = show.get("thinking")
    values = meta.get("values") if isinstance(meta, dict) else None
    if isinstance(values, list) and values:
        if all(value is False for value in values):
            return ThinkingProfile(THINKING_NONE, basis="Ollama: thinking.values [false]")
        if any(value is False for value in values):
            return ThinkingProfile(THINKING_CAN_DISABLE, THINKING_OFF_OLLAMA_THINK,
                                   basis=f"Ollama: thinking.values {values}")
        return ThinkingProfile(THINKING_ALWAYS,
                               basis=f"Ollama: thinking.values {values} false içermiyor")
    caps = show.get("capabilities")
    names = {str(item).lower() for item in caps} if isinstance(caps, list) else None
    if names is not None and "thinking" in names:
        return ThinkingProfile(THINKING_UNKNOWN,
                               basis="Ollama: 'thinking' yeteneği var, thinking üst verisi yok")
    if names is None:
        return ThinkingProfile(THINKING_UNKNOWN, basis="Ollama: yetenek listesi yok")
    # Yetenek yok: Ollama "think" alanını bu modele uygulamaz (resmî yol yok).
    profile = template_profile(show.get("template"), backend="", soft_switch=False)
    if profile.cls == THINKING_UNKNOWN:
        return ThinkingProfile(THINKING_NONE, basis="Ollama: 'thinking' yeteneği yok")
    return profile


# ---------------------------------------------------------------------------
# Sohbet şablonu (MLX klasörü, GGUF dosyası)
# ---------------------------------------------------------------------------
def template_text(template: Any) -> Optional[str]:
    """Şablon metni: düz metin ya da ``[{"name", "template"}]`` listesinin birleşimi."""
    if isinstance(template, str):
        return template if template.strip() else None
    if isinstance(template, list):
        parts = [item.get("template") for item in template if isinstance(item, dict)]
        joined = "\n".join(p for p in parts if isinstance(p, str))
        return joined if joined.strip() else None
    return None


def template_profile(template: Any, *, backend: str, soft_switch: bool) -> ThinkingProfile:
    """Sohbet şablonundan sınıf.

    - şablon yok/boş: "bilinmiyor";
    - düşünme izi yok: "yok";
    - MLX + ``enable_thinking`` anahtarı: "kapatilabilir" (çevirmen
      ``chat_template_kwargs`` ile ``enable_thinking=False`` geçirir);
    - Qwen3 karma model (``soft_switch``): "kapatilabilir" ("/no_think");
    - diğer düşünen şablonlar: "kapatilamaz" (ör. llama-cpp-python sohbet
      şablonuna ``enable_thinking`` geçiremez — 0.3.x
      ``create_chat_completion``'da bu parametre yok).
    """
    text = template_text(template)
    if text is None:
        return ThinkingProfile(THINKING_UNKNOWN, basis="sohbet şablonu yok ya da okunamadı")
    has_switch = _SWITCH_MARK in text
    if not has_switch and not any(mark in text for mark in _THINK_MARKS):
        return ThinkingProfile(THINKING_NONE, basis="sohbet şablonunda düşünme izi yok")
    if backend == BACKEND_MLX and has_switch:
        return ThinkingProfile(THINKING_CAN_DISABLE, THINKING_OFF_TEMPLATE_SWITCH,
                               basis="şablonda enable_thinking anahtarı (apply_chat_template)")
    if soft_switch:
        return ThinkingProfile(THINKING_CAN_DISABLE, THINKING_OFF_QWEN3_SOFT,
                               basis="Qwen3 karma model: '/no_think' yumuşak anahtarı")
    return ThinkingProfile(THINKING_ALWAYS,
                           basis="şablon düşünüyor; bu arka uçta resmî kapatma yolu yok")


def qwen3_soft_switch(names: Iterable[Any]) -> bool:
    """Adlardan biri Qwen3 karma modeli mi (``backends.supports_no_think`` kuralı)."""
    from .backends import supports_no_think   # çağrı anında: döngüsel içe aktarma olmasın
    return any(supports_no_think(SimpleNamespace(model=name))
               for name in names if isinstance(name, str) and name)


def profile_of(model: Any) -> ThinkingProfile:
    """Keşif kaydının (``DiscoveredModel``) sınıfı — model seçicinin okuma noktası.

    ``thinking`` None ise (model bulunamadı / sunucu kapalı) HY-MT biçiminde
    "yok", aksi "bilinmiyor".
    """
    cls = getattr(model, "thinking", None)
    if cls is None:
        if getattr(model, "format_guess", None) == FORMAT_HYMT:
            return ThinkingProfile(THINKING_NONE, basis="HY-MT biçimi düşünmez")
        return _UNKNOWN
    return ThinkingProfile(str(cls), getattr(model, "thinking_off", None) or None)


# ---------------------------------------------------------------------------
# Kapatma doğrulaması (yapışkan bayrak)
# ---------------------------------------------------------------------------
_LOG_OFF_FAILED = ("[MT DÜŞÜNME] %s: düşünme kapatma isteğine (%s) rağmen yanıtta düşünce "
                   "geldi (%d token); model bu koşu için 'kapatilamaz' sayılıyor (yapışkan)")
_LOG_UNEXPECTED = ("[MT DÜŞÜNME] %s: düşünme sınıfı '%s' olan modelde düşünce ölçüldü "
                   "(%d token); resmî kapatma yolu yok, model bu koşu için 'kapatilamaz' "
                   "sayılıyor (yapışkan)")
_LOG_OFF_UNAVAILABLE = ("[MT DÜŞÜNME] %s: resmî düşünme kapatma yolu (%s) kullanılamadı "
                        "(%s); model bu koşu için 'kapatilamaz' sayılıyor (yapışkan)")


class ThinkingOffMonitor:
    """Kapatma doğrulaması: kapatma tutmadıysa model bu koşu için "kapatilamaz".

    Koşu boyu tek nesne (arka uç başına; düşünen MLX modelinde çevirmen
    başına). ``set_profile`` yüklemede statik sınıfı koyar. Her üretim
    denemesinin sonucu ``observe(result, off_requested=...)``'dan AYNEN geçer:

    - kapatma isteği gönderilmiş denemenin yanıtında gerçek düşünce
      (``GenerationResult.thinking_substantive``; boş ``<think></think>``
      sayılmaz) ölçülürse ``off_failed`` YAPIŞKAN olur;
    - sınıfı "yok" / "bilinmiyor" olan ya da kapatma yolu kullanılamayan
      modelde düşünce ölçülürse ``unexpected`` YAPIŞKAN olur;
    - ``mark_off_unavailable``: resmî yol sunucuda reddedildi / yok (ör.
      LM Studio /api/v1/chat 404); model düşüneceği için hemen yapışkan.

    Her olay günlüğe BİR kez yazılır. ``effective_class`` bayrak varsa
    "kapatilamaz", yoksa statik sınıftır. Düşünerek çeviri kipi (G2b-2,
    ``backends.MTBackend.thinking_mode`` / ``BookTranslator._mlx_thinking_mode``)
    bunu her denemede okur: bayrak konduğu denemenin yeniden denemesi bile
    düşünme payıyla yapılır. Bu nesne bütçe, yeniden deneme ve devre kesiciye
    dokunmaz. İş parçacığı güvenlidir (toplu üretim havuzu).
    """

    def __init__(self, label: Callable[[], str] | str = ""):
        self._label = label
        self._lock = threading.Lock()
        self.profile = _UNKNOWN
        self.off_failed = False         # kapatma isteğine rağmen düşünce geldi
        self.unexpected = False         # kapatma yolu olmayan modelde düşünce ölçüldü
        self.off_unavailable: Optional[str] = None   # resmî yol kullanılamadı (neden)
        self.first_tokens = 0           # bayrağı koyan denemenin düşünme token'ı

    # ---------------------------------------------------------------- durum
    def label(self) -> str:
        try:
            return self._label() if callable(self._label) else str(self._label)
        except Exception:   # etiket üretimi bekçiyi düşürmesin
            return "model"

    def set_profile(self, profile: ThinkingProfile) -> None:
        with self._lock:
            self.profile = profile if isinstance(profile, ThinkingProfile) else _UNKNOWN

    @property
    def sticky(self) -> bool:
        """Koşu için "kapatilamaz" hükmü verildi mi (geri dönmez)."""
        return bool(self.off_failed or self.unexpected or self.off_unavailable)

    @property
    def effective_class(self) -> str:
        return THINKING_ALWAYS if self.sticky else self.profile.cls

    # ---------------------------------------------------------------- olaylar
    def mark_off_unavailable(self, path: str, reason: str) -> None:
        with self._lock:
            if self.off_unavailable:
                return
            self.off_unavailable = reason or "neden bilinmiyor"
        logger.warning(_LOG_OFF_UNAVAILABLE, self.label(), path, self.off_unavailable)

    def observe(self, result: Any, *, off_requested: bool) -> Any:
        """Deneme sonucunu incele (sonuç değişmeden döner)."""
        if not isinstance(result, GenerationResult) or result.thinking_tokens <= 0:
            return result
        if not getattr(result, "thinking_substantive", True):
            return result      # boş düşünme bloğu (ör. "/no_think" yanıtı): düşünce değil
        cls = self.profile.cls
        if cls == THINKING_ALWAYS:
            return result      # beklenen düşünme
        message: Optional[Tuple[str, Tuple[Any, ...]]] = None
        with self._lock:
            if off_requested:
                if not self.off_failed:
                    self.off_failed = True
                    self.first_tokens = self.first_tokens or result.thinking_tokens
                    message = (_LOG_OFF_FAILED,
                               (self.profile.off_path or "resmî kapatma isteği",
                                result.thinking_tokens))
            elif cls != THINKING_CAN_DISABLE or self.off_unavailable:
                # Kapatma yolu yok (ya da kullanılamadı) ve model düşünüyor.
                if not self.unexpected:
                    self.unexpected = True
                    self.first_tokens = self.first_tokens or result.thinking_tokens
                    message = (_LOG_UNEXPECTED, (cls, result.thinking_tokens))
        if message is not None:
            template, args = message
            logger.warning(template, self.label(), *args)
        return result

    def snapshot(self) -> dict:
        """Durumun düz sözlüğü (günlük / G2b-2 / test)."""
        return {"class": self.profile.cls, "off_path": self.profile.off_path,
                "effective_class": self.effective_class, "off_failed": self.off_failed,
                "unexpected": self.unexpected, "off_unavailable": self.off_unavailable,
                "first_tokens": self.first_tokens}

