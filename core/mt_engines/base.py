"""MT motor katmanı — model-özel davranışların strateji arayüzü.

Neden var (2026-07-27): HY-MT'ye özel her şey (prompt formatı, özel token
temizliği, model yolu, dil adları) core/translator.py gövdesine gömülüydü.
Yeni/daha iyi bir çeviri modeli çıktığında kod ameliyatı gerekiyordu. Bu
katman "model-özel atomları" tek arayüzde toplar:

- BookTranslator'ın 3 aydır sertleştirilen üretim mekaniği (kilitler, OOM
  bölme, batch_generate fallback'leri, timeout'lar, prompt-leak temizliği)
  MODEL-BAĞIMSIZDIR ve core/translator.py'de KALIR.
- Motor (MTEngine) yalnızca şunları bilir: modelin diskte/Hugging Face'te nerede
  olduğu, prompt'un nasıl kurulduğu, çıktıdan hangi özel token'ların
  süzüleceği, hangi dilleri desteklediği.

Yeni model eklemek için tipik yol: modeli MLX'e çevir, config/mt_engines.json
dosyasına bir kayıt ekle (format: "chat" çoğu instruct modeli kapsar).
Ayrıntı: docs/YENI_MODEL_EKLEME.md
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from .contracts import BACKEND_MLX, default_chat_template_kwargs

# Özel token dizgilerinin genel biçimi: <|ad|> (HY-MT <|extra_0|>, ChatML
# <|im_end|>, <|endoftext|> ...). Kaynak metinde geçen böyle bir dizgi
# prompt'a kaçışsız gömülürse GERÇEK özel token'a tokenize olur (bkz.
# MTEngine.sanitize_input).
_SPECIAL_TOKEN_RE = re.compile(r"<\|[^\s<>|]{0,64}\|>")

# Talimat metninde kullanılan dil adları (ISO 639-1 -> İngilizce ad).
# core/translator.py'deki _HYMT_LANG_NAMES ile aynı tablo — motor kendi
# tablosunu vermezse bu kullanılır.
DEFAULT_LANG_NAMES: Dict[str, str] = {
    "en": "English", "tr": "Turkish", "de": "German", "fr": "French",
    "es": "Spanish", "it": "Italian", "pt": "Portuguese", "ru": "Russian",
    "ar": "Arabic", "zh": "Chinese", "ja": "Japanese", "ko": "Korean",
}


@dataclass
class EngineSpec:
    """Bir çeviri motorunun kayıt bilgisi (registry girdisi).

    ``name`` registry anahtarıdır (ör. "hymt-7b"); ``format`` ise prompt
    biçimini uygulayan sınıfı seçer (registry.ENGINE_FORMATS anahtarı).
    Aynı format sınıfıyla farklı model dosyalarını (farklı ``local_dir`` /
    ``model_id``) kaydetmek mümkündür — kod değişikliği gerekmez.
    """

    name: str
    format: str = "hymt"
    model_id: str = ""            # HuggingFace kimliği (transformers + convert komutu)
    local_dir: str = ""           # models/<local_dir> altındaki MLX dizini
    description: str = ""
    languages: Dict[str, str] = field(default_factory=dict)
    strip_tokens: List[str] = field(default_factory=list)  # çıktıdan süzülecek ek token'lar
    source: str = "builtin"       # contracts.SPEC_SOURCES: "builtin" | "user"
                                  # (config/mt_engines.json) | "adhoc" (önekli kimlik)
    # --- Arka uç alanları (2026-09-25; sözleşme: core/mt_engines/contracts.py) ---
    # Varsayılanlar bugünkü MLX yolunu AYNEN tarif eder: backend "mlx",
    # sunucu/yol alanları boş. MLX spec'lerinde bu alanlar okunmaz
    # (chat_template_kwargs hariç: MLX chat biçiminde apply_chat_template'e geçer).
    backend: str = BACKEND_MLX    # contracts.BACKENDS
    model: str = ""               # ollama etiketi / openai model kimliği
    base_url: str = ""            # ollama: "http://127.0.0.1:11434"; openai: ".../v1"
                                  # (registry check_local_url'den geçirir)
    path: str = ""                # mlx klasörü ya da .gguf dosyası için DOĞRULANMIŞ mutlak yol
    flavor: str = ""              # openai için contracts.FLAVOR_LMSTUDIO | "" (genel)
    options: Dict[str, Any] = field(default_factory=dict)
        # izinli anahtarlar: contracts.ENGINE_OPTION_KEYS (num_ctx, keep_alive,
        # max_parallel, n_ctx, n_gpu_layers, temperature, repeat_penalty, seed,
        # request_timeout_s)
    extra_body: Dict[str, Any] = field(default_factory=dict)   # yalnız openai: istek gövdesine eklenir
    think: Optional[bool] = False  # False: düşünmeyi kapatmayı dene; None: dokunma
    chat_template_kwargs: Dict[str, Any] = field(default_factory=default_chat_template_kwargs)
        # MLX apply_chat_template'e ve openai istek gövdesinde "chat_template_kwargs"
        # olarak geçer (Jinja şablonu değişkeni kullanmıyorsa yok sayılır)
    system_prompt: bool = False   # chat: talimat system rolünde mi (varsayılan: tek user mesajı)
    raw: bool = False             # format "hymt" + ollama/openai: ham tamamlama ucu
                                  # (/api/generate raw, /completions) — deneysel,
                                  # yalnız mt_engines.json kaydıyla açılır (TASARIM B.8)


class MTEngine:
    """Model-özel strateji: yükleme yolu + prompt biçimi + çıktı temizliği.

    Üretim çağrılarının kendisi (mlx_lm.generate / batch_generate /
    transformers generate, kilitler, OOM bölme) BookTranslator'dadır;
    motor yalnızca girdi/çıktı biçimini ve model konumunu bilir.
    """

    def __init__(self, spec: EngineSpec):
        self.spec = spec

    # ------------------------------------------------------------------ #
    # Model konumu
    # ------------------------------------------------------------------ #
    def resolve_local_model_dir(self, models_dir: Path) -> Optional[Path]:
        """models/ altında MLX model dizinini bul.

        ``spec.path`` doluysa (registry izinli kökte doğruladı; ``mlx:``
        kimliği ya da mt_engines.json ``path`` alanı) ve dizin varsa o döner.
        Aksi hâlde önce ``<local_dir>``, sonra ``<local_dir>.nosync`` denenir
        (iCloud senkronundan kaçınma deseni — models/hymt-7b-mlx bir .nosync
        symlink'idir). Bulunamazsa None.
        """
        if self.spec.path:
            explicit = Path(self.spec.path)
            if explicit.exists():
                return explicit
        if not self.spec.local_dir:
            return None
        candidate = models_dir / self.spec.local_dir
        if candidate.exists():
            return candidate
        nosync = models_dir / (self.spec.local_dir + ".nosync")
        if nosync.exists():
            return nosync
        return None

    def convert_hint(self, models_dir: Path) -> str:
        """Model diskte yoksa kullanıcıya gösterilecek dönüştürme komutu."""
        target = models_dir / (self.spec.local_dir or (self.spec.name + "-mlx"))
        src = self.spec.model_id or "<huggingface-model-id>"
        return f"mlx_lm.convert --hf-path {src} --mlx-path {target}"

    # ------------------------------------------------------------------ #
    # Prompt / çıktı biçimi
    # ------------------------------------------------------------------ #
    def input_strip_tokens(self) -> List[str]:
        """Girdi metninde nötralize edilecek özel token dizgileri.

        Varsayılan: motorun çıktıdan süzdüğü token'lar. ``<|...|>`` biçimli
        olanlar zaten genel desenle yakalanır; buradaki liste ``</s>``,
        ``[INST]`` gibi farklı biçimli token'lar içindir.
        """
        return [t for t in self.spec.strip_tokens if t]

    @staticmethod
    def _defuse_token(token: str) -> str:
        """Token dizgisini içeriği kaybetmeden eşleşemez hale getir."""
        return token[:1] + " " + token[1:] if len(token) > 1 else token

    def sanitize_input(self, text: str) -> str:
        """Prompt'a gömülecek metni özel-token enjeksiyonundan arındır.

        Hugging Face/MLX tokenizer'ları metin İÇİNDEKİ literal ``<|extra_0|>`` gibi
        dizgileri gerçek özel token id'sine çevirir (``add_special_tokens=
        False`` bunu engellemez — o yalnız baş/son eklerini kontrol eder).
        Kaynak kitapta böyle bir dizgi geçerse (LLM/ML kitaplarında ChatML
        örnekleri yaygın) üretim-başlangıcı işareti girdinin ortasına
        enjekte olur: çeviri o noktada kesilir veya talimat echo'lanır.

        Silmek yerine ilk karakterden sonra boşluk eklenir (``<|extra_0|>``
        -> ``< |extra_0|>``): içerik okunur kalır, segment boşalmaz, dizgi
        hiçbir özel token'la eşleşmez. İşlem idempotenttir.
        """
        if not text:
            return text
        result = _SPECIAL_TOKEN_RE.sub(lambda m: self._defuse_token(m.group(0)), text)
        for tok in self.input_strip_tokens():
            if tok in result:
                result = result.replace(tok, self._defuse_token(tok))
        return result

    def build_mlx_prompt(self, tokenizer, instruction: str, text: str) -> str:
        """MLX generate için ham prompt dizgisi kur. Alt sınıf doldurur."""
        raise NotImplementedError

    def build_raw_prompt(self, instruction: str, text: str) -> str:
        """Tokenizer'sız ham prompt (ham tamamlama uçları için).

        Ollama ``/api/generate`` + ``raw``, OpenAI uyumlu ``/completions`` ve
        llama-cpp-python ``create_completion`` bunu kullanır (TASARIM B.8;
        deneysel). Yalnız ham özel-token biçimi olan motorlar doldurur
        (HYMTFormatEngine); sohbet biçimi şablonu sunucuya/GGUF'a bırakır.
        """
        raise NotImplementedError(
            f"'{self.spec.format}' biçimi ham tamamlama prompt'u kurmaz")

    def build_chat_messages(self, instruction: str, text: str) -> List[Dict[str, str]]:
        """Transformers ``apply_chat_template`` için mesaj listesi.

        Varsayılan biçim (tek user mesajı: talimat + boş satır + metin)
        mevcut HY-MT transformers yolunun birebir aynısıdır ve çoğu
        instruct modeli için doğrudur. İçerik ``sanitize_input``'tan geçer:
        aksi halde metindeki ``<|im_end|>`` template'i erken kapatır.
        """
        content = f"{self.sanitize_input(instruction)}\n\n{self.sanitize_input(text)}"
        return [{"role": "user", "content": content}]

    def clean_output(self, raw: str, source: str = "") -> str:
        """Üretim çıktısından model-özel token kalıntılarını süz.

        ``source``: çevrilen özgün metin (verilmezse ""). Taban sınıf onu
        kullanmaz; sohbet biçimi (ChatTemplateEngine) kaynağa bakarak
        önsöz / sarmalayıcı temizliğinin meşru içeriği silmesini önler.

        NOT: [?]/unknown-token onarımı ve prompt-leak temizliği motorun işi
        DEĞİLDİR — onlar model-bağımsızdır ve BookTranslator'da kalır.
        """
        result = raw.strip()
        for tok in self.spec.strip_tokens:
            if tok:
                result = result.replace(tok, "")
        return result.strip()

    # ------------------------------------------------------------------ #
    # Dil desteği
    # ------------------------------------------------------------------ #
    def lang_name(self, code: str, default: Optional[str] = None) -> str:
        """Talimat metninde kullanılacak dil adı (ör. "en" -> "English")."""
        table = self.spec.languages or DEFAULT_LANG_NAMES
        if default is None:
            default = "English" if code == "en" else "Turkish"
        return table.get(code, default)

    def supports_lang(self, code: str) -> bool:
        table = self.spec.languages or DEFAULT_LANG_NAMES
        return code in table
