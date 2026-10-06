"""HY-MT format motoru — mevcut (varsayılan) davranışın birebir taşınması.

Bu sınıf core/translator.py'nin 2026-07-27 öncesi gövdesine gömülü olan
HY-MT-özel biçimi üretir; davranış DEĞİŞMEMİŞTİR:

- MLX prompt'u: ``<|startoftext|>{talimat}<|extra_4|>{metin}<|extra_0|>``
  (<|extra_4|> sistem talimatını kullanıcı metninden ayırır; model talimatı
  çevrilecek içerik sanmaz)
- Çıktı temizliği: 4 özel token (<|eos|>, <|extra_0|>, <|extra_4|>,
  <|startoftext|>) süzülür
- Transformers yolu: taban sınıfın tek-user-mesajlı chat biçimi
- Ham tamamlama arka uçları (2026-09-25; llama.cpp ``create_completion``,
  Ollama ``/api/generate`` raw, OpenAI ``/completions``): ``build_raw_prompt``
  MLX prompt'uyla AYNI dizeyi verir. Sohbet uçlarında (Ollama ``/api/chat``,
  OpenAI ``/chat/completions``) taban ``build_chat_messages`` kullanılır —
  transformers yolunun birebir aynısı (TASARIM B.8).
"""

from __future__ import annotations

from typing import List

from .base import EngineSpec, MTEngine

# HY-MT üretim çıktısından süzülen özel token'lar (sıra korunmuştur)
HYMT_SPECIAL_TOKENS: List[str] = [
    "<|eos|>", "<|extra_0|>", "<|extra_4|>", "<|startoftext|>",
]


class HYMTFormatEngine(MTEngine):
    """HY-MT1.5 ailesinin özel-token prompt biçimi."""

    def __init__(self, spec: EngineSpec):
        super().__init__(spec)
        # Kullanıcı spec'i ek strip token verdiyse HY-MT setinin üstüne eklenir
        merged = list(HYMT_SPECIAL_TOKENS)
        for tok in spec.strip_tokens:
            if tok and tok not in merged:
                merged.append(tok)
        self._strip = merged

    def input_strip_tokens(self) -> List[str]:
        # Girdi temizliğinde HY-MT'nin tam token seti kullanılır
        return list(self._strip)

    def build_mlx_prompt(self, tokenizer, instruction: str, text: str) -> str:
        # tokenizer bilerek kullanılmaz: HY-MT ham özel-token biçimi
        # chat template'e ihtiyaç duymaz (imza, chat tabanlı motorlarla
        # ortak olsun diye tokenizer alır).
        # Metin/talimat sanitize_input'tan geçer: kaynakta geçen literal
        # <|extra_0|> prompt'u ortadan bölerdi (sessiz içerik kaybı).
        safe_instruction = self.sanitize_input(instruction)
        safe_text = self.sanitize_input(text)
        return f"<|startoftext|>{safe_instruction}<|extra_4|>{safe_text}<|extra_0|>"

    def build_raw_prompt(self, instruction: str, text: str) -> str:
        # Ham tamamlama uçları için MLX prompt'uyla birebir aynı dize
        # (tokenizer'a ihtiyaç yok; bkz. build_mlx_prompt).
        return self.build_mlx_prompt(None, instruction, text)

    def clean_output(self, raw: str, source: str = "") -> str:
        # source bilerek kullanılmaz: HY-MT temizliği değişmez (yalnız özel token).
        result = raw.strip()
        for tok in self._strip:
            result = result.replace(tok, "")
        return result.strip()
