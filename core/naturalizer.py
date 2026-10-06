"""
Turkce Dogallastirici (Turkish Naturalizer)
AI olmadan kural tabanli Turkce dogallastirma
"""

import re
from typing import List, Tuple


class TurkishNaturalizer:
    """
    AI olmadan Turkce dogallastirma.

    Kurallar:
    - Edilgen -> Etken donusumu
    - Gereksiz yumusatici kelimeleri kaldir
    - Ingilizce cumle yapisi duzeltmeleri
    - Tekrar eden kelimeleri temizle
    """

    # Gereksiz yumusatici kelimeler (cok sik kullanilirsa cikar)
    FILLER_WORDS = [
        (r'\baslinda\b', ''),
        (r'\baslında\b', ''),
        (r'\bgerçekten\b', ''),
        (r'\bgercekten\b', ''),
        (r'\besasen\b', ''),
        (r'\baçıkçası\b', ''),
        (r'\bacikçasi\b', ''),
        (r'\bkesinlikle\b', ''),  # Bazen gereksiz
    ]

    # Edilgen -> Etken donusumleri
    PASSIVE_TO_ACTIVE = [
        # NOT: "X tarafindan Y-lmistir" -> "Y-di" kurallari KALDIRILDI.
        # Regex ile edilgen->etken donusumu guvenli yapilamiyor: eslesen
        # span'in tamami tek fiille degistirildigi icin OZNE siliniyordu
        # ("Deney Smith tarafindan yapilmistir." -> "Deney yapidi.") ve
        # uretilen fiil cekimi Turkce'de yoktu (audit 2026-07-16 #14).
        # "edilmektedir" -> basit hal
        (r'edilmektedir', 'edilir'),
        (r'yapılmaktadır', 'yapılır'),
        (r'yapilmaktadir', 'yapilir'),
        (r'görülmektedir', 'görülür'),
        (r'gorulmektedir', 'gorulur'),
        (r'bulunmaktadır', 'bulunur'),
        (r'bulunmaktadir', 'bulunur'),
    ]

    # Ingilizce kaynaklı yaygın hatalar
    COMMON_FIXES = [
        # "bir şekilde" gereksiz kullanimi. \b sart: sol sinir olmadan
        # "hiçbir şekilde" ifadesinin ici yakalanip kelimeler kaynasiyor,
        # "Bunu hiçbir şekilde kabul etmez" -> "Bunu hiçkabul etmez"
        # oluyordu (audit 2026-07-16 #27).
        (r'\bbir şekilde\s+', ''),
        (r'\bbir sekilde\s+', ''),
        # 2026-09-24: "oldukça" -> "epey" kuralı KALDIRILDI. "epey" konuşma
        # dilidir; akademik metinde kipi bozuyordu (bir test koşusu: 4 vuruş,
        # "epey küçüktür", "epey önemlidir"; Yol A referansı "oldukça").
        # Çift boşluk temizle
        (r'\s{2,}', ' '),
        # Noktalama öncesi boşluk
        (r'\s+([.,;:!?])', r'\1'),
    ]

    # Turkce'de dogal olmayan kaliplar
    UNNATURAL_PATTERNS = [
        # "Bu ... dir" -> "... dir" (gereksiz "Bu")
        (r'^Bu\s+(.+?)\s+(dır|dir|tır|tir|dur|dür)\s*\.', r'\1 \2.'),
        # "O ... dır" -> "... dır"
        (r'^O\s+(.+?)\s+(dır|dir|tır|tir|dur|dür)\s*\.', r'\1 \2.'),
    ]

    def __init__(self, aggressive: bool = False):
        """
        Args:
            aggressive: True ise daha fazla degisiklik yapar
        """
        self.aggressive = aggressive

    def naturalize(self, text: str) -> str:
        """
        Metni dogallastir

        Args:
            text: Cevrilmis Turkce metin

        Returns:
            Dogallastirilmis metin
        """
        if not text or len(text) < 10:
            return text

        result = text

        # 1. Cift bosluklari temizle
        result = ' '.join(result.split())

        # 2. Edilgen -> Etken (dikkatli)
        for pattern, replacement in self.PASSIVE_TO_ACTIVE:
            result = re.sub(pattern, replacement, result)

        # 3. Yaygın hataları düzelt
        for pattern, replacement in self.COMMON_FIXES:
            result = re.sub(pattern, replacement, result)

        # 4. Agresif mod: yumusatici kelimeleri cikar
        if self.aggressive:
            for pattern, replacement in self.FILLER_WORDS:
                result = re.sub(pattern, replacement, result, flags=re.IGNORECASE)

        # 5. Dogal olmayan kaliplari duzelt
        for pattern, replacement in self.UNNATURAL_PATTERNS:
            result = re.sub(pattern, replacement, result)

        # 6. Tekrar eden kelimeleri temizle (ardisik)
        result = self._remove_repeated_words(result)

        # 7. Son temizlik
        result = ' '.join(result.split())

        return result

    def _remove_repeated_words(self, text: str) -> str:
        """Ardisik tekrar eden kelimeleri kaldir (yalniz 3+ tekrar).

        Turkce'de ikileme mesru bir yapidir ("yavas yavas", "ayri ayri"):
        ikili tekrara DOKUNULMAZ. Yalnizca MT stutter'i olan 3 ve uzeri
        ardisik tekrar tekile indirilir (audit 2026-07-16 #27).
        """
        words = text.split()
        result = []
        run_word = None
        run_len = 0

        for word in words:
            if run_word is not None and word.lower() == run_word.lower():
                run_len += 1
                # Ikileme (2 tekrar) korunur; 3+ tekrar MT stutter'idir
                if run_len <= 2:
                    result.append(word)
                continue
            run_word = word
            run_len = 1
            result.append(word)

        return ' '.join(result)

    def fix_word_order(self, text: str) -> str:
        """
        Turkce cumle yapisina uygun hale getir (SOV)

        NOT: Bu cok zor bir problem, basit regex ile tam cozulemez.
        Sadece belirgin hatalari duzeltir.
        """
        # Basit durum: "X Y Z eder" -> zaten dogru
        # Sorunlu: "eder X Y Z" -> "X Y Z eder"

        # Fiil basta ise sona tasi (cok basit heuristic)
        verb_at_start = re.match(r'^(etmek|yapmak|olmak|gelmek|gitmek|almak|vermek)\s+(.+)$', text, re.IGNORECASE)
        if verb_at_start:
            verb = verb_at_start.group(1)
            rest = verb_at_start.group(2)
            return f"{rest} {verb}"

        return text


# Test
if __name__ == "__main__":
    naturalizer = TurkishNaturalizer(aggressive=True)

    test_texts = [
        "Bu aslında oldukça önemli bir konudur.",
        "Araştırma tarafından yapılmıştır.",
        "Bu  cümle   çift  boşluk  içeriyor .",
        "Gerçekten bu oldukça ilginç bir durumdur.",
        "Bu konu hakkında hakkında konuştuk.",
    ]

    print("Turkce Dogallastirici Test\n" + "="*50)
    for text in test_texts:
        result = naturalizer.naturalize(text)
        print(f"Önce:  {text}")
        print(f"Sonra: {result}")
        print("-"*50)
