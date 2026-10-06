"""
Tutarlılık Kontrolü (Consistency Checker)
Aynı terimin farklı şekillerde çevrilmesini tespit ve düzeltme
"""

from collections import defaultdict
from typing import Dict, List, Tuple, Optional
from dataclasses import dataclass, field


@dataclass
class InconsistencyIssue:
    """Tutarsızlık sorunu"""
    source_term: str
    translations: Dict[str, int]  # {çeviri: sayı}
    recommended: str  # Önerilen çeviri
    segments: List[int]  # Etkilenen segment indexleri


@dataclass
class ConsistencyReport:
    """Tutarlılık raporu"""
    total_terms: int = 0
    inconsistent_terms: int = 0
    issues: List[InconsistencyIssue] = field(default_factory=list)
    auto_fixed: int = 0

    @property
    def consistency_score(self) -> float:
        """Tutarlılık skoru (0-100)"""
        if self.total_terms == 0:
            return 100.0
        return round((1 - self.inconsistent_terms / self.total_terms) * 100, 1)

    def summary(self) -> str:
        lines = [
            "Tutarlılık Raporu",
            "=" * 40,
            f"Toplam terim: {self.total_terms}",
            f"Tutarsız terim: {self.inconsistent_terms}",
            f"Tutarlılık skoru: {self.consistency_score}/100",
            f"Otomatik düzeltilen: {self.auto_fixed}",
        ]

        if self.issues:
            lines.append("\nTutarsızlıklar:")
            for issue in self.issues[:10]:
                translations_str = ", ".join(
                    f"'{t}' ({c}x)" for t, c in sorted(
                        issue.translations.items(),
                        key=lambda x: -x[1]
                    )
                )
                lines.append(f"  '{issue.source_term}' → {translations_str}")
                lines.append(f"    Önerilen: '{issue.recommended}'")

        return "\n".join(lines)


class ConsistencyChecker:
    """
    Çeviri tutarlılığı kontrolü

    - Aynı terimin farklı çevirilmesini tespit eder
    - En sık kullanılan çeviriyi önerir
    - Otomatik düzeltme yapabilir
    """

    def __init__(self, glossary_manager=None):
        """
        Args:
            glossary_manager: GlossaryManager (terim tanıma için)
        """
        self.glossary = glossary_manager

        # İç durumu tutma
        self.term_translations: Dict[str, Dict[str, List[int]]] = defaultdict(
            lambda: defaultdict(list)
        )  # {source_term: {translation: [segment_indices]}}

    def reset(self):
        """Durumu sıfırla"""
        self.term_translations = defaultdict(lambda: defaultdict(list))

    def track_translation(self, source: str, target: str, segment_idx: int):
        """
        Çeviriyi takip et

        Args:
            source: Kaynak metin
            target: Hedef metin
            segment_idx: Segment indexi
        """
        if not self.glossary:
            return

        # Kaynak metindeki terimleri bul
        source_terms = self.glossary.find_terms_in_text(source)

        for term_info in source_terms:
            term_en = term_info["term_en"].lower()
            expected_tr = term_info["term_tr"].lower()

            # Hedef metinde hangi çeviri kullanılmış?
            target_lower = target.lower()

            # Önce beklenen Türkçe terimi ara
            if expected_tr in target_lower:
                self.term_translations[term_en][expected_tr].append(segment_idx)
            # Sonra orijinal İngilizce terimi ara (çevrilmemiş olabilir)
            elif term_en in target_lower:
                self.term_translations[term_en][term_en].append(segment_idx)
            else:
                # Hedefte ne var kontrol et - fuzzy match denemesi
                # Basit: kelime kelime ara
                words_in_target = set(target_lower.split())
                found_translation = None

                for word in term_info["term_tr"].lower().split():
                    if word in words_in_target and len(word) > 3:
                        found_translation = word
                        break

                if found_translation:
                    self.term_translations[term_en][found_translation].append(segment_idx)
                else:
                    # Bilinmeyen çeviri
                    self.term_translations[term_en]["[?]"].append(segment_idx)

    def analyze(self) -> ConsistencyReport:
        """
        Tutarlılık analizi yap

        Returns:
            ConsistencyReport
        """
        report = ConsistencyReport(total_terms=len(self.term_translations))

        for source_term, translations in self.term_translations.items():
            if len(translations) > 1:
                # Tutarsızlık var
                report.inconsistent_terms += 1

                # En sık kullanılan çeviriyi bul
                translation_counts = {
                    tr: len(indices) for tr, indices in translations.items()
                }
                recommended = max(translation_counts.keys(), key=lambda x: translation_counts[x])

                # Tüm etkilenen segmentleri topla
                all_segments = []
                for indices in translations.values():
                    all_segments.extend(indices)

                report.issues.append(InconsistencyIssue(
                    source_term=source_term,
                    translations=translation_counts,
                    recommended=recommended,
                    segments=sorted(set(all_segments))
                ))

        return report

    def get_corrections(self) -> Dict[int, List[Tuple[str, str]]]:
        """
        Düzeltme önerilerini getir

        Returns:
            {segment_idx: [(yanlış_terim, doğru_terim), ...]}
        """
        corrections: Dict[int, List[Tuple[str, str]]] = defaultdict(list)

        for source_term, translations in self.term_translations.items():
            if len(translations) <= 1:
                continue

            # En sık kullanılan GERÇEK çeviriyi bul. '[?]' (eşleşme
            # bulunamadı kovası) öneri OLAMAZ — eskiden max() onu
            # seçebiliyor ve gerçek çeviriler literal '[?]' ile
            # değiştiriliyordu (#16).
            translation_counts = {
                tr: len(indices) for tr, indices in translations.items()
                if tr != "[?]"
            }
            if not translation_counts:
                continue
            recommended = max(translation_counts.keys(), key=lambda x: translation_counts[x])

            # Diğer çevirileri kullanan segmentleri düzelt
            for wrong_translation, segment_indices in translations.items():
                if wrong_translation != recommended and wrong_translation != "[?]":
                    for idx in segment_indices:
                        corrections[idx].append((wrong_translation, recommended))

        return dict(corrections)

    def apply_corrections(self, segments: List[Dict]) -> Tuple[List[Dict], int]:
        """
        Düzeltmeleri uygula

        Args:
            segments: Çevrilmiş segmentler

        Returns:
            (düzeltilmiş_segmentler, düzeltme_sayısı)
        """
        corrections = self.get_corrections()
        fixed_count = 0

        for idx, segment_corrections in corrections.items():
            if idx >= len(segments):
                continue

            text = segments[idx]["text"]

            for wrong, correct in segment_corrections:
                if wrong in text.lower():
                    # Case-insensitive, KELİME SINIRLI replace: sınırsız
                    # substring 'güç'->'kuvvet' düzeltmesinde 'güçlü'yü
                    # 'kuvvetlü' yapıyordu (#16). İlk harfin büyüklüğü
                    # korunur.
                    import re

                    def _cased(m: "re.Match", _c: str = correct) -> str:
                        found = m.group(0)
                        if found[:1].isupper():
                            return _c[:1].upper() + _c[1:]
                        return _c

                    new_text, n = re.subn(
                        r"\b" + re.escape(wrong) + r"\b",
                        _cased,
                        text,
                        flags=re.IGNORECASE,
                    )
                    if n:
                        text = new_text
                        fixed_count += 1

            segments[idx]["text"] = text

        return segments, fixed_count


class DocumentConsistencyAnalyzer:
    """
    Doküman seviyesinde tutarlılık analizi

    Çeviri sırasında veya sonrasında kullanılabilir
    """

    def __init__(self, glossary_manager=None):
        self.glossary = glossary_manager
        self.checker = ConsistencyChecker(glossary_manager)

    def analyze_document(self,
                         source_segments: List[str],
                         target_segments: List[str]) -> ConsistencyReport:
        """
        Tüm dokümanı analiz et

        Args:
            source_segments: Kaynak metinler listesi
            target_segments: Hedef metinler listesi

        Returns:
            ConsistencyReport
        """
        self.checker.reset()

        for idx, (source, target) in enumerate(zip(source_segments, target_segments)):
            self.checker.track_translation(source, target, idx)

        return self.checker.analyze()

    def fix_inconsistencies(self,
                            segments: List[Dict],
                            auto_fix: bool = True) -> Tuple[List[Dict], ConsistencyReport]:
        """
        Tutarsızlıkları tespit et ve düzelt

        Args:
            segments: [{"text": "...", "original": "...", ...}, ...]
            auto_fix: Otomatik düzeltme yap mı?

        Returns:
            (düzeltilmiş_segmentler, rapor)
        """
        # Önce analiz
        self.checker.reset()

        for idx, seg in enumerate(segments):
            source = seg.get("original", "")
            target = seg.get("text", "")
            self.checker.track_translation(source, target, idx)

        report = self.checker.analyze()

        # Düzeltme
        if auto_fix and report.inconsistent_terms > 0:
            segments, fixed_count = self.checker.apply_corrections(segments)
            report.auto_fixed = fixed_count

        return segments, report


# Test
if __name__ == "__main__":
    # Basit test (glossary olmadan)
    checker = ConsistencyChecker()

    # Manuel track
    checker.term_translations["force"]["kuvvet"].extend([0, 2, 5])
    checker.term_translations["force"]["güç"].extend([1, 3])
    checker.term_translations["momentum"]["momentum"].extend([0, 1, 2, 3, 4])

    report = checker.analyze()
    print(report.summary())

    print("\nDüzeltmeler:")
    corrections = checker.get_corrections()
    for idx, fixes in corrections.items():
        print(f"  Segment {idx}: {fixes}")
