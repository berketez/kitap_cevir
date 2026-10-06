"""
Placeholder Manager - Formul ve Kod Koruma Sistemi

Ceviri sirasinda korunmasi gereken icerikleri (formuller, kod bloklari,
ozel karakterler) placeholder ile degistirip, ceviri sonrasi geri koyar.

Kullanim:
    pm = PlaceholderManager()
    protected_text = pm.protect(original_text)
    # ... ceviri yap ...
    final_text = pm.restore(translated_text)
"""

import re
from typing import Dict, List, Tuple, Optional
from dataclasses import dataclass, field
from enum import Enum
import logging

logger = logging.getLogger(__name__)


class PlaceholderType(Enum):
    """Placeholder turleri"""
    FORMULA_INLINE = "formula_inline"      # $...$
    FORMULA_DISPLAY = "formula_display"    # $$...$$ veya \[...\]
    FORMULA_ENV = "formula_env"            # \begin{equation}...\end{equation}
    CODE_INLINE = "code_inline"            # `...`
    CODE_BLOCK = "code_block"              # ```...```
    REFERENCE = "reference"                # Eq. 3.4, Fig. 5, Table 2
    URL = "url"                            # http://... veya https://...
    VARIABLE = "variable"                  # Tek harfli degiskenler: x, y, z
    UNIT = "unit"                          # kg, m/s, N/m^2


@dataclass
class PlaceholderEntry:
    """Bir placeholder kaydi"""
    placeholder: str
    original: str
    ptype: PlaceholderType
    start_pos: int = 0
    end_pos: int = 0


@dataclass
class PlaceholderManager:
    """
    Ceviri sirasinda korunacak icerikleri yonetir.

    Ozellikler:
    - LaTeX formullerini korur ($, $$, \\begin{...})
    - Kod bloklarini korur (`, ```)
    - Referanslari korur (Eq., Fig., Table)
    - URL'leri korur
    - Tek harfli degiskenleri korur (opsiyonel)

    Attributes:
        placeholder_format: Placeholder formati (varsayilan: "{{v{id}}}")
        protect_variables: Tek harfli degiskenleri koru (varsayilan: False)
    """

    placeholder_format: str = "{{{{v{id}}}}}"  # {{v0}}, {{v1}}, ...
    protect_variables: bool = False
    protect_units: bool = True

    # Internal state
    _entries: Dict[str, PlaceholderEntry] = field(default_factory=dict)
    _counter: int = 0
    _original_text: str = ""

    def __post_init__(self):
        self._entries = {}
        self._counter = 0

    def reset(self):
        """State'i sifirla"""
        self._entries = {}
        self._counter = 0
        self._original_text = ""

    def _generate_placeholder(self) -> str:
        """Yeni placeholder olustur"""
        ph = self.placeholder_format.format(id=self._counter)
        self._counter += 1
        return ph

    def _add_entry(self, original: str, ptype: PlaceholderType) -> str:
        """Yeni entry ekle ve placeholder dondur"""
        placeholder = self._generate_placeholder()
        self._entries[placeholder] = PlaceholderEntry(
            placeholder=placeholder,
            original=original,
            ptype=ptype
        )
        return placeholder

    # =========================================================================
    # PATTERN TANIMLARI
    # =========================================================================

    # LaTeX formul pattern'leri
    PATTERNS = {
        # Display math: $$...$$ (cok satirli olabilir)
        'formula_display_double': (
            r'\$\$[\s\S]+?\$\$',
            PlaceholderType.FORMULA_DISPLAY
        ),

        # Display math: \[...\]
        'formula_display_bracket': (
            r'\\\[[\s\S]+?\\\]',
            PlaceholderType.FORMULA_DISPLAY
        ),

        # Inline math: $...$ (tek satir, ic ice $ yok)
        'formula_inline': (
            r'(?<!\$)\$(?!\$)(?:[^$\n]|\\\$)+?\$(?!\$)',
            PlaceholderType.FORMULA_INLINE
        ),

        # LaTeX environments: \begin{equation}...\end{equation}
        'formula_env': (
            r'\\begin\{(equation|align|gather|multline|eqnarray)\*?\}[\s\S]+?\\end\{\1\*?\}',
            PlaceholderType.FORMULA_ENV
        ),

        # Inline code: `...`
        'code_inline': (
            r'`[^`\n]+?`',
            PlaceholderType.CODE_INLINE
        ),

        # Code block: ```...```
        'code_block': (
            r'```[\s\S]+?```',
            PlaceholderType.CODE_BLOCK
        ),

        # References: Eq. 3.4, Fig. 5, Table 2, Chapter 3, Section 2.1
        'reference': (
            r'\b(?:Eq(?:uation)?|Fig(?:ure)?|Table|Chapter|Section|Appendix|Theorem|Lemma|Corollary|Definition|Proposition|Remark|Example|Algorithm)\s*\.?\s*[\d]+(?:\.[\d]+)*(?:\s*[-–]\s*[\d]+(?:\.[\d]+)*)?',
            PlaceholderType.REFERENCE
        ),

        # URLs
        'url': (
            r'https?://[^\s\)\]\}]+',
            PlaceholderType.URL
        ),
    }

    # Birim pattern'leri
    UNIT_PATTERNS = {
        'unit_with_number': (
            # Sayi + birim: 5 kg, 3.14 m/s, 100 N/m^2
            r'\b\d+(?:\.\d+)?\s*(?:kg|g|mg|km|m|cm|mm|nm|μm|s|ms|μs|ns|Hz|kHz|MHz|GHz|N|kN|Pa|kPa|MPa|GPa|J|kJ|MJ|W|kW|MW|V|mV|kV|A|mA|μA|Ω|kΩ|MΩ|F|μF|nF|pF|H|mH|μH|C|K|°C|°F|mol|rad|sr|lm|lx|Bq|Gy|Sv|kat|eV|keV|MeV|GeV|TeV|m/s|m/s²|km/h|rad/s|N/m|N/m²|kg/m³|J/K|W/m²|A/m|V/m|T|Wb|cd|mol/L|g/mol)(?:\^[0-9-]+)?(?:/(?:kg|g|m|cm|s|K|mol)(?:\^[0-9-]+)?)*\b',
            PlaceholderType.UNIT
        ),
    }

    # =========================================================================
    # KORUMA METODLARI
    # =========================================================================

    def protect(self, text: str) -> str:
        """
        Metindeki korunacak icerikleri placeholder ile degistir.

        Args:
            text: Orijinal metin

        Returns:
            Placeholder'li metin
        """
        self.reset()
        self._original_text = text
        result = text

        # Pattern'leri uygula (oncelik sirasina gore)
        # Once buyuk pattern'ler, sonra kucukler
        pattern_order = [
            'code_block',           # ``` once
            'formula_env',          # \begin{} once
            'formula_display_double',  # $$ once
            'formula_display_bracket', # \[ once
            'formula_inline',       # $ sonra
            'code_inline',          # ` sonra
            'reference',            # Eq., Fig. vs
            'url',                  # URL'ler
        ]

        for pattern_name in pattern_order:
            if pattern_name not in self.PATTERNS:
                continue

            pattern, ptype = self.PATTERNS[pattern_name]

            def replacer(match):
                original = match.group(0)
                return self._add_entry(original, ptype)

            result = re.sub(pattern, replacer, result, flags=re.IGNORECASE)

        # Birim koruma (opsiyonel)
        if self.protect_units:
            for pattern_name, (pattern, ptype) in self.UNIT_PATTERNS.items():
                def replacer(match):
                    original = match.group(0)
                    return self._add_entry(original, ptype)

                result = re.sub(pattern, replacer, result)

        logger.debug(f"Korunan element sayisi: {len(self._entries)}")

        return result

    def restore(self, text: str) -> str:
        """
        Placeholder'lari orijinal iceriklerle degistir.

        Args:
            text: Placeholder'li cevrilmis metin

        Returns:
            Restore edilmis metin
        """
        result = text

        # Placeholder'lari geri koy - guclendirilmis regex ile
        for placeholder, entry in self._entries.items():
            # Exact match. Basariliysa varyant asamasi ATLANIR: restore
            # edilen orijinalin icinde 'vN' gecmesi ($v0$ hiz degiskeni)
            # varyant regex'inin az once yerlestirilen icerigi yeniden
            # degistirip cogaltmasina yol aciyordu (#17).
            if placeholder in result:
                result = result.replace(placeholder, entry.original)
                continue

            # Bozulmus placeholder varyantlari icin regex (ceviri sırasında bozulabilir)
            # {{v0}} -> { {v0} }, {{v 0}}, {{ v0 }}, {v0}, vb.
            placeholder_id = re.search(r'v(\d+)', placeholder)
            if placeholder_id:
                vid = placeholder_id.group(1)
                # Olasi bozulma pattern'leri
                variants = [
                    rf'\{{\s*\{{\s*v\s*{vid}\s*\}}\s*\}}',  # {{ v 0 }}
                    rf'\{{\s*v\s*{vid}\s*\}}',              # { v0 }
                    rf'(?<![A-Za-z])v{vid}(?![A-Za-z0-9])',  # v0 (boundary korumali - "version", "Chapter v1" yutulmasin)
                ]
                for variant in variants:
                    # NOT: lambda sart — entry.original replacement-string
                    # olarak yorumlanirsa '\t' TAB'e donusuyor, '\1'
                    # re.error firlatiyordu (#17).
                    result = re.sub(variant, lambda m: entry.original, result)

        return result

    def protect_and_translate(
        self,
        text: str,
        translate_func: callable
    ) -> str:
        """
        Metni koru, cevir ve restore et (tek adimda).

        Args:
            text: Orijinal metin
            translate_func: Ceviri fonksiyonu (str -> str)

        Returns:
            Cevrilmis ve restore edilmis metin
        """
        protected = self.protect(text)
        translated = translate_func(protected)
        restored = self.restore(translated)
        return restored

    # =========================================================================
    # YARDIMCI METODLAR
    # =========================================================================

    def get_entries(self) -> List[PlaceholderEntry]:
        """Tum entry'leri dondur"""
        return list(self._entries.values())

    def get_entries_by_type(self, ptype: PlaceholderType) -> List[PlaceholderEntry]:
        """Belirli turdeki entry'leri dondur"""
        return [e for e in self._entries.values() if e.ptype == ptype]

    def get_stats(self) -> Dict[str, int]:
        """Istatistikleri dondur"""
        stats = {}
        for entry in self._entries.values():
            ptype_name = entry.ptype.value
            stats[ptype_name] = stats.get(ptype_name, 0) + 1
        return stats

    def has_formulas(self) -> bool:
        """Metin formul iceriyor mu?"""
        formula_types = {
            PlaceholderType.FORMULA_INLINE,
            PlaceholderType.FORMULA_DISPLAY,
            PlaceholderType.FORMULA_ENV
        }
        return any(e.ptype in formula_types for e in self._entries.values())

    def has_code(self) -> bool:
        """Metin kod iceriyor mu?"""
        code_types = {
            PlaceholderType.CODE_INLINE,
            PlaceholderType.CODE_BLOCK
        }
        return any(e.ptype in code_types for e in self._entries.values())


# =============================================================================
# OZEL PLACEHOLDER YONETICILERI
# =============================================================================

class TechnicalPlaceholderManager(PlaceholderManager):
    """Teknik kitaplar icin ozellestirilmis placeholder yoneticisi"""

    def __init__(self):
        super().__init__()
        self.protect_variables = False
        self.protect_units = True

        # Ek teknik pattern'ler
        self.PATTERNS['greek_letter'] = (
            r'\\(?:alpha|beta|gamma|delta|epsilon|zeta|eta|theta|iota|kappa|lambda|mu|nu|xi|omicron|pi|rho|sigma|tau|upsilon|phi|chi|psi|omega|Gamma|Delta|Theta|Lambda|Xi|Pi|Sigma|Upsilon|Phi|Psi|Omega)\b',
            PlaceholderType.FORMULA_INLINE
        )


class CodePlaceholderManager(PlaceholderManager):
    """Programlama kitaplari icin ozellestirilmis placeholder yoneticisi"""

    def __init__(self):
        super().__init__()

        # Ek kod pattern'leri
        self.PATTERNS['function_call'] = (
            r'\b[a-zA-Z_][a-zA-Z0-9_]*\s*\([^)]*\)',
            PlaceholderType.CODE_INLINE
        )


# =============================================================================
# TEST VE DEMO
# =============================================================================

if __name__ == "__main__":
    logging.basicConfig(level=logging.DEBUG)

    # Test metni (teknik kitaptan ornek)
    test_text = """
    Residual networks (ResNets) appeared in the work of He et al. (2015).
    The gradient $\\partial L / \\partial \\theta$ can become very small.

    Consider the following equation:
    $$
    y = f(x) + x
    $$

    This is known as Eq. 3.1 and is fundamental to understanding ResNets.
    The learning rate is typically set to 0.001 or 1e-3.

    See Figure 5.2 for the architecture diagram.
    The model processes data at 100 MB/s with 32 GB memory.

    Example code:
    ```python
    def forward(x):
        return self.layer(x) + x
    ```

    For inline code, use `model.train()` to enable training mode.

    Reference: https://arxiv.org/abs/1512.03385
    """

    print("=" * 60)
    print("Placeholder Manager Test")
    print("=" * 60)

    pm = PlaceholderManager()

    print("\n[ORIJINAL METIN]")
    print(test_text[:200] + "...")

    protected = pm.protect(test_text)

    print("\n[KORUNAN METIN]")
    print(protected[:200] + "...")

    print("\n[ISTATISTIKLER]")
    for ptype, count in pm.get_stats().items():
        print(f"  {ptype}: {count}")

    print("\n[KORUNAN ICERIKLER]")
    for entry in pm.get_entries()[:5]:
        print(f"  {entry.placeholder} -> {entry.original[:50]}...")

    # Simule ceviri (sadece buyuk harfe cevir)
    def fake_translate(text):
        # Placeholder'lari atla, gerisi buyuk harf
        import re
        def upper_non_placeholder(match):
            return match.group(0)

        result = text.upper()
        # Placeholder'lari geri koy (kucuk harfle kalmisti)
        for ph in pm._entries.keys():
            result = result.replace(ph.upper(), ph)
        return result

    translated = fake_translate(protected)
    restored = pm.restore(translated)

    print("\n[RESTORE EDILMIS METIN]")
    print(restored[:300] + "...")

    print("\n" + "=" * 60)
    print("Test tamamlandi!")
