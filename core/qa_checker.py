"""
Kalite Kontrol (QA Checker)
Çeviri kalitesini kontrol eder ve raporlar
"""

import re
from dataclasses import dataclass, field
from typing import List, Dict, Optional, Tuple
from collections import Counter

# Tanınan birim dizgileri (QAChecker.UNIT_PATTERN'in kaynağı).
# Desene UZUN-ÖNCE sıralı girer: Python alternation soldan sağa ilk
# BAŞARILI dalda durur ve '/' karakteri \b sınırını sağladığı için tek
# harfli 'm', bileşik 'm/s'den önce gelirse '50 m/s' -> 'm' eşleşiyordu;
# m/s, km/h, kg/m³, N/m gibi alternatifler hiçbir girdide seçilemiyor,
# birim-kaybı denetimi bu birimlerde kör kalıyordu (F45).
_UNIT_TOKENS: List[str] = [
    # Bileşik birimler (uzun-önce sıralama zaten öne alır, liste başında
    # tutulmaları niyeti belgeler)
    "m/s²", "m/s", "km/h", "kg/m³", "g/cm³", "N/m²", "N/m",
    # Kütle
    "kg", "g", "mg", "μg",
    # Uzunluk
    "m", "cm", "mm", "km", "nm", "μm",
    # Zaman
    "s", "ms", "μs", "ns", "min", "h",
    # Frekans
    "Hz", "kHz", "MHz", "GHz",
    # Kuvvet
    "N", "kN", "MN",
    # Enerji
    "J", "kJ", "MJ", "eV", "keV", "MeV", "GeV",
    # Güç
    "W", "kW", "MW",
    # Basınç
    "Pa", "kPa", "MPa", "GPa", "bar", "atm",
    # Sıcaklık
    "K", "°C", "°F",
    # Madde miktarı
    "mol",
    # Elektrik
    "A", "mA", "μA", "V", "mV", "kV", "Ω", "kΩ", "MΩ",
    # Açı
    "rad", "deg", "°",
]


@dataclass
class QAIssue:
    """Kalite sorunu"""
    category: str  # number, unit, term, reference, missing, formatting
    severity: str  # error, warning, info
    message: str
    source_text: str = ""
    target_text: str = ""
    position: int = -1
    suggestion: str = ""


@dataclass
class QAReport:
    """Kalite raporu"""
    total_segments: int = 0
    checked_segments: int = 0
    issues: List[QAIssue] = field(default_factory=list)
    stats: Dict[str, int] = field(default_factory=dict)

    @property
    def error_count(self) -> int:
        return sum(1 for i in self.issues if i.severity == "error")

    @property
    def warning_count(self) -> int:
        return sum(1 for i in self.issues if i.severity == "warning")

    @property
    def quality_score(self) -> float:
        """0-100 arası kalite skoru"""
        if self.checked_segments == 0:
            return 100.0

        # Hata ağırlıkları
        error_weight = 10
        warning_weight = 3

        penalty = (self.error_count * error_weight +
                   self.warning_count * warning_weight)

        max_penalty = self.checked_segments * error_weight
        score = max(0, 100 - (penalty / max_penalty * 100)) if max_penalty > 0 else 100

        return round(score, 1)

    def to_dict(self) -> dict:
        return {
            "total_segments": self.total_segments,
            "checked_segments": self.checked_segments,
            "error_count": self.error_count,
            "warning_count": self.warning_count,
            "quality_score": self.quality_score,
            "issues": [
                {
                    "category": i.category,
                    "severity": i.severity,
                    "message": i.message,
                    "source": i.source_text[:100] if i.source_text else "",
                    "target": i.target_text[:100] if i.target_text else "",
                    "suggestion": i.suggestion
                }
                for i in self.issues
            ],
            "stats": self.stats
        }

    def summary(self) -> str:
        """Özet rapor"""
        lines = [
            f"Kalite Raporu",
            f"{'='*40}",
            f"Toplam segment: {self.total_segments}",
            f"Kontrol edilen: {self.checked_segments}",
            f"Kalite skoru: {self.quality_score}/100",
            f"",
            f"Hatalar: {self.error_count}",
            f"Uyarılar: {self.warning_count}",
        ]

        if self.stats:
            lines.append(f"\nİstatistikler:")
            for key, value in self.stats.items():
                lines.append(f"  {key}: {value}")

        if self.issues:
            lines.append(f"\nSorunlar:")
            for issue in self.issues[:10]:  # İlk 10
                icon = "X" if issue.severity == "error" else "!"
                lines.append(f"  [{icon}] {issue.category}: {issue.message}")

        if len(self.issues) > 10:
            lines.append(f"  ... ve {len(self.issues) - 10} sorun daha")

        return "\n".join(lines)


class QAChecker:
    """
    Çeviri kalite kontrolü

    Kontroller:
    - Sayıların korunması
    - Birimlerin korunması
    - Terim tutarlılığı
    - Referans numaralarının eşleşmesi
    - Eksik çeviri
    - Format tutarlılığı
    """

    # Sayı pattern'ı
    NUMBER_PATTERN = re.compile(
        r'(?<![a-zA-Z])'  # Harf öncelememeli
        r'-?\d+(?:[.,]\d+)?'  # Tam veya ondalık sayı
        r'(?:\s*[×x]\s*10\s*[\^]?\s*[-]?\d+)?'  # Bilimsel notasyon
        r'(?![a-zA-Z])'  # Harf takip etmemeli
    )

    # Birim pattern'ı — alternatifler _UNIT_TOKENS'tan UZUN-ÖNCE üretilir
    # (F45: bileşik birimler tek harflilerden önce gelmeli). sorted stabil
    # olduğu için eşit uzunluktakiler liste sırasını korur.
    UNIT_TOKENS = _UNIT_TOKENS
    UNIT_PATTERN = re.compile(
        r'\b\d+(?:\.\d+)?\s*('
        + '|'.join(re.escape(u) for u in sorted(_UNIT_TOKENS, key=len, reverse=True))
        + r')\b',
        re.IGNORECASE
    )

    # Referans pattern'ı — Türkçe eşdeğerler de tanınır (#23): 'Eq. 5'
    # doğru şekilde 'Denklem 5' çevrildiğinde eskiden hedefte desen
    # eşleşmeyip sahte 'Referans eksik' hatası üretiliyordu. findall
    # yalnızca numarayı döndürdüğü için iki dilin numara kümeleri
    # doğrudan karşılaştırılabilir.
    REFERENCE_PATTERN = re.compile(
        r'\b(?:Eq|Eqs|Equation|Figure|Fig|Table|Ref|Section|Chapter'
        r'|Denklem|Denk|Şekil|Sekil|Tablo|Çizelge|Cizelge|Kaynak'
        r'|Bölüm|Bolum|Kısım|Kisim|Ek)'
        r'\.?\s*[\[\(]?(\d+(?:[.,]\d+)?(?:\s*[-–]\s*\d+)?)'
        r'(?:[a-z])?[\]\)]?',
        re.IGNORECASE
    )

    def __init__(self, glossary_manager=None):
        """
        Args:
            glossary_manager: GlossaryManager instance (terim kontrolü için)
        """
        self.glossary = glossary_manager
        self.report = QAReport()

    def check_segment(self, source: str, target: str, segment_id: int = 0) -> List[QAIssue]:
        """
        Tek bir segment'i kontrol et

        Args:
            source: Kaynak metin (İngilizce)
            target: Hedef metin (Türkçe)
            segment_id: Segment numarası (raporlama için)

        Returns:
            Bulunan sorunların listesi
        """
        issues = []

        # Boş çeviri kontrolü
        if not target or not target.strip():
            issues.append(QAIssue(
                category="missing",
                severity="error",
                message="Çeviri boş",
                source_text=source,
                position=segment_id
            ))
            return issues

        # Sayı kontrolü
        issues.extend(self._check_numbers(source, target, segment_id))

        # Birim kontrolü
        issues.extend(self._check_units(source, target, segment_id))

        # Referans kontrolü
        issues.extend(self._check_references(source, target, segment_id))

        # Terim kontrolü (glossary varsa)
        if self.glossary:
            issues.extend(self._check_terms(source, target, segment_id))

        # Format kontrolü
        issues.extend(self._check_formatting(source, target, segment_id))

        # Uzunluk kontrolü
        issues.extend(self._check_length(source, target, segment_id))

        return issues

    def check_document(self, segments: List[Tuple[str, str]]) -> QAReport:
        """
        Tüm dokümanı kontrol et

        Args:
            segments: [(kaynak, hedef), ...] listesi

        Returns:
            QAReport
        """
        self.report = QAReport(
            total_segments=len(segments),
            checked_segments=0
        )

        all_source_terms = Counter()
        all_target_terms = Counter()

        for i, (source, target) in enumerate(segments):
            issues = self.check_segment(source, target, i)
            self.report.issues.extend(issues)
            self.report.checked_segments += 1

            # Terim sayımı (tutarlılık için)
            if self.glossary:
                for term_info in self.glossary.find_terms_in_text(source):
                    all_source_terms[term_info["term_en"]] += term_info["count"]
                for term_info in self.glossary.find_terms_in_text(target):
                    all_target_terms[term_info["term_tr"]] += term_info["count"]

        # İstatistikler
        self.report.stats = {
            "total_numbers_source": sum(
                len(self.NUMBER_PATTERN.findall(s)) for s, _ in segments
            ),
            "total_numbers_target": sum(
                len(self.NUMBER_PATTERN.findall(t)) for _, t in segments
            ),
            "unique_terms_source": len(all_source_terms),
            "unique_terms_target": len(all_target_terms),
        }

        return self.report

    def _check_numbers(self, source: str, target: str, segment_id: int) -> List[QAIssue]:
        """Sayıların korunduğunu kontrol et"""
        issues = []

        source_numbers = set(self.NUMBER_PATTERN.findall(source))
        target_numbers = set(self.NUMBER_PATTERN.findall(target))

        # Kaynakta olup hedefte olmayan
        missing = source_numbers - target_numbers
        for num in missing:
            # Virgül/nokta farkını tolere et
            normalized = num.replace(',', '.')
            if not any(n.replace(',', '.') == normalized for n in target_numbers):
                issues.append(QAIssue(
                    category="number",
                    severity="error",
                    message=f"Sayı eksik: {num}",
                    source_text=source,
                    target_text=target,
                    position=segment_id
                ))

        return issues

    def _check_units(self, source: str, target: str, segment_id: int) -> List[QAIssue]:
        """Birimlerin korunduğunu kontrol et"""
        issues = []

        source_units = self.UNIT_PATTERN.findall(source)
        target_units = self.UNIT_PATTERN.findall(target)

        # Birim sayısı farklıysa
        if len(source_units) != len(target_units):
            issues.append(QAIssue(
                category="unit",
                severity="warning",
                message=f"Birim sayısı farklı: kaynak={len(source_units)}, hedef={len(target_units)}",
                source_text=source,
                target_text=target,
                position=segment_id
            ))

        # Eksik birimler
        source_unit_set = set(u.lower() for u in source_units)
        target_unit_set = set(u.lower() for u in target_units)

        missing = source_unit_set - target_unit_set
        for unit in missing:
            issues.append(QAIssue(
                category="unit",
                severity="error",
                message=f"Birim eksik: {unit}",
                source_text=source,
                target_text=target,
                position=segment_id
            ))

        return issues

    def _check_references(self, source: str, target: str, segment_id: int) -> List[QAIssue]:
        """Referans numaralarının eşleştiğini kontrol et"""
        issues = []

        source_refs = set(self.REFERENCE_PATTERN.findall(source))
        target_refs = set(self.REFERENCE_PATTERN.findall(target))

        # Eksik referanslar
        missing = source_refs - target_refs
        for ref in missing:
            issues.append(QAIssue(
                category="reference",
                severity="error",
                message=f"Referans eksik: {ref}",
                source_text=source,
                target_text=target,
                position=segment_id
            ))

        return issues

    def _check_terms(self, source: str, target: str, segment_id: int) -> List[QAIssue]:
        """Teknik terimlerin doğru çevrildiğini kontrol et"""
        issues = []

        if not self.glossary:
            return issues

        # Kaynaktaki terimleri bul
        source_terms = self.glossary.find_terms_in_text(source)

        for term_info in source_terms:
            expected_tr = term_info["term_tr"].lower()

            # Hedefte Türkçe karşılık var mı?
            if expected_tr not in target.lower():
                # Orijinal terim de yoksa sorun
                if term_info["term_en"].lower() not in target.lower():
                    issues.append(QAIssue(
                        category="term",
                        severity="warning",
                        message=f"Terim çevirisi eksik: '{term_info['term_en']}' → '{term_info['term_tr']}'",
                        source_text=source,
                        target_text=target,
                        position=segment_id,
                        suggestion=f"Beklenen: {term_info['term_tr']}"
                    ))

        return issues

    def _check_formatting(self, source: str, target: str, segment_id: int) -> List[QAIssue]:
        """Format tutarlılığını kontrol et"""
        issues = []

        # Parantez dengesi
        for char_open, char_close in [('(', ')'), ('[', ']'), ('{', '}')]:
            source_balance = source.count(char_open) - source.count(char_close)
            target_balance = target.count(char_open) - target.count(char_close)

            if source_balance == 0 and target_balance != 0:
                issues.append(QAIssue(
                    category="formatting",
                    severity="warning",
                    message=f"Parantez dengesi bozuk: {char_open}{char_close}",
                    source_text=source,
                    target_text=target,
                    position=segment_id
                ))

        # Satır sonu noktalama
        source_end = source.strip()[-1] if source.strip() else ""
        target_end = target.strip()[-1] if target.strip() else ""

        if source_end in '.!?' and target_end not in '.!?':
            issues.append(QAIssue(
                category="formatting",
                severity="info",
                message="Cümle sonu noktalama eksik",
                source_text=source,
                target_text=target,
                position=segment_id
            ))

        return issues

    def _check_length(self, source: str, target: str, segment_id: int) -> List[QAIssue]:
        """Uzunluk oranını kontrol et"""
        issues = []

        source_len = len(source.strip())
        target_len = len(target.strip())

        if source_len == 0:
            return issues

        ratio = target_len / source_len

        # Türkçe genelde İngilizce'den %10-30 daha uzun
        # Çok kısa veya çok uzun çeviriler şüpheli
        if ratio < 0.5:
            issues.append(QAIssue(
                category="length",
                severity="warning",
                message=f"Çeviri çok kısa (oran: {ratio:.1%})",
                source_text=source,
                target_text=target,
                position=segment_id
            ))
        elif ratio > 2.5:
            issues.append(QAIssue(
                category="length",
                severity="warning",
                message=f"Çeviri çok uzun (oran: {ratio:.1%})",
                source_text=source,
                target_text=target,
                position=segment_id
            ))

        return issues


# Test
if __name__ == "__main__":
    checker = QAChecker()

    test_segments = [
        # Sayı hatası
        (
            "The force is 100 N and the mass is 10 kg.",
            "Kuvvet 100 N ve kütle 5 kg."  # 10 → 5 hata
        ),
        # Birim eksik
        (
            "The velocity is 50 m/s.",
            "Hız 50'dir."  # m/s eksik
        ),
        # Referans eksik
        (
            "As shown in Eq. 5, see also Fig. 3.",
            "Denklem 5'te görüldüğü gibi."  # Fig. 3 eksik
        ),
        # İyi çeviri
        (
            "Newton's second law states F = ma.",
            "Newton'un ikinci yasası F = ma olarak ifade edilir."
        ),
    ]

    report = checker.check_document(test_segments)
    print(report.summary())
