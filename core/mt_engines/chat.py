"""Genel chat-template motoru — yeni instruct modelleri için hazır format.

Çoğu modern MT/instruct modeli (Qwen, Gemma, TowerInstruct, ALMA, ...)
tokenizer'ına gömülü bir chat template ile gelir. Bu motor, prompt'u
``tokenizer.apply_chat_template`` ile kurar; yani yeni bir model için
çoğunlukla HİÇ kod yazmadan config/mt_engines.json'a şu tarz bir kayıt
yeter::

    {
      "yeni-model": {
        "format": "chat",
        "model_id": "org/Yeni-MT-7B",
        "local_dir": "yeni-model-mlx",
        "description": "Yeni model — deneme",
        "strip_tokens": ["<|im_end|>"]
      }
    }

Model çıktısında kalan sohbet-sonu token'ları ``strip_tokens`` ile süzülür.

Sunucu arka uçları (Ollama / OpenAI uyumlu / llama.cpp, 2026-09-25) aynı
motoru kullanır: şablonu sunucu ya da GGUF uygular, motor yalnız mesaj
listesini (``build_chat_messages``) ve çıktı temizliğini verir. Genel
sohbet modelleri çevirmen değildir; çıktılarında düşünme blokları, önsöz
("İşte çeviri:"), tırnağa sarma ve sona not ekleme görülür. ``clean_output``
bunları kaynak metne bakarak temizler (TASARIM G); HY-MT biçimine dokunulmaz.
"""

from __future__ import annotations

import re
from typing import Dict, List, Optional

from .base import MTEngine

# ---------------------------------------------------------------------------
# Düşünme (reasoning) blokları
# ---------------------------------------------------------------------------
# Açık adlar (THINK_*, HARMONY_*) arka uçların düşünme ölçümünde de kullanılır
# (core/mt_engines/backends.py ThinkingMeter; tek kaynak).
_THINK_OPEN = "<think>"
_THINK_CLOSE = "</think>"
_THINK_BLOCK_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
THINK_OPEN_RE = _THINK_OPEN_RE = re.compile(r"<think>", re.IGNORECASE)
THINK_CLOSE_RE = _THINK_CLOSE_RE = re.compile(r"</think>", re.IGNORECASE)
# gpt-oss "harmony" biçimi: <|channel|>analysis<|message|>…<|end|>
# <|start|>assistant<|channel|>final<|message|>YANIT — yalnız ham llama.cpp
# çıktısında görülür (sunucular kanalları kendisi ayrıştırır).
HARMONY_MARK = _HARMONY_MARK = "<|channel|>"
HARMONY_FINAL_RE = _HARMONY_FINAL_RE = re.compile(
    r"<\|channel\|>\s*final\s*<\|message\|>", re.IGNORECASE)
_HARMONY_TOKEN_RE = re.compile(r"<\|[a-z_]{1,32}\|>", re.IGNORECASE)
# Qwen3 "yumuşak anahtarı" (model kartı, Hugging Face Qwen/Qwen3-8B, 28 Eyl
# 2026): kullanıcı ya da sistem mesajındaki "/no_think" o turda düşünmeyi
# kapatır. Yalnız sert kapatmalı yeniden denemede sistem mesajı olarak eklenir
# (backends.with_no_think); model yanıta kopyalarsa clean_output siler.
NO_THINK_SWITCH = "/no_think"
_NO_THINK_SWITCH_RE = re.compile(r"(?<![\w/])/no_think\b[ \t]*")


def source_has_think(source: str) -> bool:
    """Kaynak metinde düşünme işareti geçiyor mu (varsa işaretlere dokunulmaz)."""
    src = source or ""
    return bool(THINK_OPEN_RE.search(src) or THINK_CLOSE_RE.search(src))

# ---------------------------------------------------------------------------
# Önsöz: yalnız çıktının BAŞINDA, iki nokta ile biten kalıp
# ---------------------------------------------------------------------------
_I = "[İIıi]"   # Türkçe büyük/küçük i ailesi (re.IGNORECASE İ'yi güvenle eşlemez)
# Bilerek dar: yalın "Türkçe:" etiketi yok (kaynak "Turkish: …" ile başlayan
# meşru bir satır olabilir); yalnız "çeviri" sözcüğü ya da "İşte … Türkçesi".
_TR_PREAMBLE = (
    rf"(?:{_I}şte\s+)?"
    rf"(?:(?:metn{_I}n|paragraf{_I}n|cümlen{_I}n|bölümün)\s+)?"
    r"(?:türkçe\s+)?"
    rf"çev{_I}r{_I}(?:s{_I}|n{_I}z)?"
    rf"(?:\s+şöyle(?:d{_I}r)?)?"
    rf"|{_I}şte\s+(?:metn{_I}n\s+)?türkçe(?:s{_I})?"
)
_EN_PREAMBLE = (
    r"(?:(?:sure|certainly|of\s+course|okay|ok)[!.,]?\s*)?"
    r"(?:here(?:'s|’s|\s+is)\s+(?:the\s+|your\s+|my\s+)?"
    r"(?:turkish\s+)?translation(?:\s+of\s+the\s+(?:text|paragraph|sentence))?"
    r"(?:\s+in(?:to)?\s+turkish)?"
    r"|(?:the\s+)?(?:turkish\s+)?translation(?:\s+in(?:to)?\s+turkish)?)"
)
_PREAMBLE_RE = re.compile(
    rf"^\s*(?:{_TR_PREAMBLE}|{_EN_PREAMBLE})\s*:[ \t]*(?:\n[ \t]*)*",
    re.IGNORECASE,
)

# ---------------------------------------------------------------------------
# Sona eklenen açıklama paragrafı ("\n\nNot: …", "(Note: …)")
# ---------------------------------------------------------------------------
_TRAILING_NOTE_RE = re.compile(
    rf"^\s*\(?\s*(?:not|note|notes|a[çÇ]{_I}klama|[çÇ]evirmen\s+notu|translator'?s?\s+note)\s*:",
    re.IGNORECASE,
)
_SOURCE_NOTE_RE = re.compile(r"\bnot(?:e|es)?\s*:|\bnote\b", re.IGNORECASE)
_PARAGRAPH_SPLIT_RE = re.compile(r"\n[ \t]*\n")

# Kaynağın başında "çeviri" anlamlı bir sözcük geçiyorsa (dilbilim / çeviri
# kitabı: "Its translation: …") çıktıdaki "Çevirisi:" meşru içeriktir; önsöz
# sanılıp silinmez. Kökler: en, de, fr/es/it, ru, tr.
_SOURCE_TRANSLATION_WORD_RE = re.compile(r"translat|übersetz|tradu[cz]|перев|çevir",
                                         re.IGNORECASE)
_SOURCE_HEAD_CHARS = 80

# ---------------------------------------------------------------------------
# Sarmalayıcı: çıktının tamamını saran eşleşen tırnak / kod çiti
# ---------------------------------------------------------------------------
_WRAP_PAIRS = (('"', '"'), ("“", "”"))
_CODE_FENCE_RE = re.compile(r"^```[\w+-]*[ \t]*\n(.*?)\n?[ \t]*```$", re.DOTALL)


def _strip_reasoning(text: str, source: str) -> str:
    """Düşünme bloklarını ve harmony kanallarını kaldır.

    - ``<think>…</think>`` blokları silinir;
    - çıktı ``</think>`` ile başlıyorsa (şablon ``<think>``'i prompt'a
      koymuşsa) ilk ``</think>``'e kadar silinir;
    - kapanmamış ``<think>`` -> boş çıktı (token bütçesi düşünmeye gitmiş;
      çağıran bunu "boş çıktı" öğe hatası sayar);
    - harmony: son ``final`` kanalından sonrası alınır; hiç ``final`` yoksa boş.
    Kaynak metinde ``<think>`` geçiyorsa düşünme işaretlerine dokunulmaz.
    """
    if not text:
        return text
    if _HARMONY_MARK in text:
        finals = list(_HARMONY_FINAL_RE.finditer(text))
        if not finals:
            return ""
        text = _HARMONY_TOKEN_RE.sub("", text[finals[-1].end():])
    if source_has_think(source):
        return text
    text = _THINK_BLOCK_RE.sub("", text)
    close = _THINK_CLOSE_RE.search(text)
    if close is not None:
        text = text[close.end():]
    if _THINK_OPEN_RE.search(text):
        return ""
    return text.strip()


def _strip_switch(text: str, source: str) -> str:
    """Sert kapatmalı denemede modelin yanıta kopyaladığı "/no_think" anahtarı.

    Kaynakta aynı dizge geçiyorsa (meşru içerik) dokunulmaz.
    """
    if NO_THINK_SWITCH not in text or NO_THINK_SWITCH in (source or ""):
        return text
    return _NO_THINK_SWITCH_RE.sub("", text).strip()


def _strip_preamble(text: str, source: str) -> str:
    """Baştaki "İşte çeviri:", "Çeviri:", "Sure! Here's the translation:" önsözü.

    Yalnız arkasından gerçek içerik geliyorsa, kaynak aynı biçimde bir
    etiketle başlamıyorsa ve kaynağın başında "çeviri" anlamlı bir sözcük
    geçmiyorsa silinir (ör. "Its translation: …" -> "Çevirisi: …" meşrudur).
    İngilizce önsözleri ayrıca ``_strip_prompt_leaks`` da yakalar.
    """
    src = (source or "").strip()
    if _PREAMBLE_RE.match(src) or _SOURCE_TRANSLATION_WORD_RE.search(src[:_SOURCE_HEAD_CHARS]):
        return text
    m = _PREAMBLE_RE.match(text)
    if m is None:
        return text
    rest = text[m.end():]
    return rest if rest.strip() else text


def _paragraph_count(text: str) -> int:
    return sum(1 for part in _PARAGRAPH_SPLIT_RE.split(text or "") if part.strip())


def _strip_trailing_note(text: str, source: str) -> str:
    """Boş satırdan sonra gelen ve kaynakta karşılığı olmayan son not paragrafı.

    Yalnız model paragraf EKLEDİYSE (çıktıda kaynaktan fazla paragraf var)
    silinir: kaynağın kendi son paragrafı ("Remark: …" -> "Not: …") meşru
    içeriktir.
    """
    if _SOURCE_NOTE_RE.search(source or ""):
        return text
    if "\n\n" not in text:
        return text
    if _paragraph_count(text) <= _paragraph_count(source):
        return text
    head, last = text.rstrip().rsplit("\n\n", 1)
    if head.strip() and _TRAILING_NOTE_RE.match(last):
        return head.rstrip()
    return text


def _strip_wrapping(text: str, source: str) -> str:
    """Çıktıyı baştan sona saran eşleşen tırnak ya da kod çitini kaldır.

    Kaynak aynı işaretle sarılıysa (ya da içte aynı tırnak geçiyorsa) dokunulmaz.
    """
    out = text.strip()
    src = (source or "").strip()
    fence = _CODE_FENCE_RE.match(out)
    if fence is not None and not src.startswith("```"):
        inner = fence.group(1).strip()
        return inner if inner else text
    for open_q, close_q in _WRAP_PAIRS:
        if len(out) < 3 or not (out.startswith(open_q) and out.endswith(close_q)):
            continue
        if src.startswith(open_q) and src.endswith(close_q):
            return text
        inner = out[len(open_q):len(out) - len(close_q)]
        if open_q in inner or close_q in inner or not inner.strip():
            return text
        return inner.strip()
    return text


class ChatTemplateEngine(MTEngine):
    """Tokenizer'ın kendi chat template'ini kullanan genel motor."""

    def build_chat_messages(self, instruction: str, text: str) -> List[Dict[str, str]]:
        """``spec.system_prompt`` True ise [system: talimat, user: metin]; değilse taban.

        Varsayılan (tek user mesajı "talimat\\n\\nmetin") değişmez.
        """
        if not self.spec.system_prompt:
            return super().build_chat_messages(instruction, text)
        return [
            {"role": "system", "content": self.sanitize_input(instruction)},
            {"role": "user", "content": self.sanitize_input(text)},
        ]

    def build_mlx_prompt(self, tokenizer, instruction: str, text: str,
                         messages: Optional[List[Dict[str, str]]] = None) -> str:
        """MLX istemi: tokenizer'ın sohbet şablonu.

        ``messages`` verilirse (düşünen MLX modelinin sert kapatmalı denemesi:
        ``backends.with_no_think``) mesajlar yeniden kurulmaz; verilmezse
        (bugünkü çağrı) ``build_chat_messages`` sonucu kullanılır.
        """
        if messages is None:
            messages = self.build_chat_messages(instruction, text)
        # Düşünme varsayılan kapalı (KARARLAR 4): spec.chat_template_kwargs
        # ({"enable_thinking": False}) şablona değişken olarak geçer; şablon
        # kullanmıyorsa yok sayılır. Şablon hata verirse sırayla: kwargs'sız
        # -> tek user mesajı -> düz birleştirme.
        template_kwargs = dict(self.spec.chat_template_kwargs or {})
        attempts = []
        if template_kwargs:
            attempts.append((messages, template_kwargs))
        attempts.append((messages, {}))
        if len(messages) > 1:
            attempts.append((MTEngine.build_chat_messages(self, instruction, text), {}))
        for msgs, extra in attempts:
            try:
                return tokenizer.apply_chat_template(
                    msgs, tokenize=False, add_generation_prompt=True, **extra,
                )
            except Exception:
                continue
        # Chat template'i olmayan/uyumsuz tokenizer: sade birleştirme.
        # (Kalite düşebilir ama pipeline çökmez; log'u çağıran basar.)
        # sanitize_input: metindeki literal özel token'lar bu yolda da
        # prompt'u bozmasın (bkz. MTEngine.sanitize_input).
        return f"{self.sanitize_input(instruction)}\n\n{self.sanitize_input(text)}\n"

    def clean_output(self, raw: str, source: str = "") -> str:
        """Sohbet modeli çıktısını temizle (TASARIM G).

        Sıra: 1) taban temizlik (strip_tokens) 2) düşünme blokları
        3) önsöz 4) sona eklenen not 5) sarmalayıcı tırnak / kod çiti.
        3-5 kaynağa bakar; ``source`` verilmezse (bugünkü MLX çağrısı)
        yalnız 1-2 uygulanır — kaynak görülmeden meşru tırnak/etiket silinmez.
        """
        result = super().clean_output(raw, source)
        result = _strip_reasoning(result, source)
        if source and source.strip() and result:
            result = _strip_switch(result, source)
            result = _strip_preamble(result, source)
            result = _strip_trailing_note(result, source)
            result = _strip_wrapping(result, source)
        return result.strip()
