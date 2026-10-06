"""
Akıllı Cümle Bölücü
Bilimsel metinler için özel kurallar içerir
"""

import re
from typing import List, Tuple

# Cümle başı olabilecek büyük harfler (2026-09-29, bug avı F3-14 / K5-2):
# Latin (aksanlılar dahil: À-Ö, Ø-Þ), Türkçe (Ğ İ Ş) ve Kiril (А-Я, Ё).
# Eskiden yalnız ASCII [A-Z] vardı: Rusça cümleler ve "À l'extérieur",
# "Über", "Äußere" ile başlayan cümleler bölünmüyordu. Harf kümesi
# latex_pipeline/translator.py _SOURCE_SENTENCE_START ile AYNI (o sabit
# bunu içe aktarmalı — tek kaynak; Y1 kulvarına not).
SENTENCE_START_UPPER = "A-ZÀ-ÖØ-ÞĞİŞА-ЯЁ"
# Cümle başında büyük harf yerine gelebilen açılış işaretleri (eski küme
# "\"'([" korunur; ters ünlem/soru ve Avrupa tırnakları eklendi).
SENTENCE_START_OPENERS = "\"'\\(\\[«„“¿¡"


class SentenceSplitter:
    """
    Bilimsel metin için gelişmiş cümle bölme

    - Kısaltmaları tanır (Dr., Prof., Fig., Eq., etc.)
    - Denklem referanslarını korur
    - Parantez içi açıklamaları doğru işler
    - Ondalık sayıları bölmez
    """

    # Cümle sonu olmayan kısaltmalar
    ABBREVIATIONS = {
        # Ünvanlar
        'dr', 'prof', 'mr', 'mrs', 'ms', 'jr', 'sr',
        # Akademik
        'fig', 'figs', 'eq', 'eqs', 'ref', 'refs', 'sec', 'secs',
        'ch', 'chap', 'vol', 'vols', 'no', 'nos', 'pp', 'ed', 'eds',
        'et al', 'ibid', 'op', 'cit', 'viz', 'cf',
        # Teknik
        'approx', 'max', 'min', 'avg', 'std', 'var',
        # Birimler
        'mm', 'cm', 'km', 'm', 'kg', 'mg', 'ml', 'hz', 'khz', 'mhz', 'ghz',
        # Genel
        'inc', 'corp', 'ltd', 'co', 'vs', 'etc', 'al',
        'i.e', 'e.g', 'ca', 'c.a',
    }

    # Denklem ve referans pattern'ları
    EQUATION_REFS = re.compile(
        r'\b(Eq|Eqs|Equation|Equations|Fig|Figs|Figure|Figures|'
        r'Table|Tables|Ref|Refs|Reference|References|'
        r'Section|Sections|Chapter|Chapters)\s*\.?\s*'
        r'[\[\(]?\d+[\]\)]?',
        re.IGNORECASE
    )

    def __init__(self, min_length: int = 10, max_length: int = 1000):
        """
        Args:
            min_length: Minimum cümle uzunluğu (karakter)
            max_length: Maksimum cümle uzunluğu (karakter)
        """
        self.min_length = min_length
        self.max_length = max_length
        self._build_patterns()

    def _build_patterns(self):
        """Regex pattern'larını oluştur"""
        # Kısaltma pattern'ı (büyük/küçük harf duyarsız)
        abbrev_pattern = '|'.join(
            re.escape(a) for a in sorted(self.ABBREVIATIONS, key=len, reverse=True)
        )
        self.abbrev_regex = re.compile(
            rf'\b({abbrev_pattern})\.(?=\s|$)',
            re.IGNORECASE
        )

        # Cümle sonu pattern'ı. Boşluk YAKALANIR: split_with_separators
        # cümleler arasındaki gerçek ayracı (boşluk / satır sonu) korur.
        self.sentence_end = re.compile(
            r'(?<=[.!?])'  # Nokta, ünlem, soru işareti sonrası
            r'(?<!\b[A-Z]\.)'  # Tek harfli kısaltma değilse (A., B., etc.)
            r'(?<!\b\d\.)'  # Numara değilse (1., 2., etc.)
            r'(\s+)'  # Boşluk (ayraç)
            # Büyük harf (Unicode; F3-14) veya açılış işareti ile başlayan
            r'(?=[' + SENTENCE_START_UPPER + SENTENCE_START_OPENERS + r'])'
        )

    def split(self, text: str) -> List[str]:
        """
        Metni cümlelere böl

        Args:
            text: Bölünecek metin

        Returns:
            Cümle listesi
        """
        return [sent for sent, _sep in self.split_with_separators(text)]

    def split_with_separators(self, text: str) -> List[Tuple[str, str]]:
        """Cümleler ve her cümleden SONRAKİ ayraç: ``[(cümle, ayraç), ...]``.

        Ayraç özgün boşluktur (yatay boşluk tek boşluğa sadeleşir; satır sonu
        ve paragraf arası korunur); son öğede "". Parçaları geri birleştiren
        (çeviri sonrası) bu ayraçları kullanmalıdır: satır sonu tek boşluğa
        inerse "% yorum" satırından sonraki cümle yorumun içine düşer
        (2026-09-29, bug avı F3-14 / K5-2).
        """
        if not text or not text.strip():
            return []

        # Ön işleme
        text = self._preprocess(text)

        # Koruma: Kısaltmaları placeholder'la değiştir
        text, abbrev_map = self._protect_abbreviations(text)

        # Koruma: Denklem referanslarını koru
        text, eq_map = self._protect_equation_refs(text)

        # Koruma: Ondalık sayıları koru
        text, decimal_map = self._protect_decimals(text)

        # Koruma: Parantez içlerini koru
        text, paren_map = self._protect_parentheses(text)

        # Cümlelere böl
        sentences = self._do_split(text)

        # Geri yükle
        restored: List[List[str]] = []   # [cümle, ardından gelen ayraç]
        for sent, sep in sentences:
            sent = self._restore_map(sent, paren_map)
            sent = self._restore_map(sent, decimal_map)
            sent = self._restore_map(sent, eq_map)
            sent = self._restore_map(sent, abbrev_map)

            # Temizle. min_length altindaki cumle ATILMAZ — bir onceki
            # cumleyle birlestirilir. Eski davranis kisa cumleleri ("He ran.",
            # "Stop now.") ceviri ciktisindan sessizce siliyordu (icerik
            # kaybi, audit 2026-07-16 #15). Birleştirmede aradaki özgün ayraç
            # kullanılır (satır sonu korunur).
            sent = sent.strip()
            if not sent:
                continue
            if len(sent) >= self.min_length or not restored:
                restored.append([sent, sep])
            else:
                restored[-1][0] = restored[-1][0] + (restored[-1][1] or " ") + sent
                restored[-1][1] = sep

        # Çok uzun cümleleri böl
        final: List[Tuple[str, str]] = []
        for sent, sep in restored:
            if len(sent) > self.max_length:
                pieces = self._split_long_pairs(sent)
                pieces[-1] = (pieces[-1][0], sep)
                final.extend(pieces)
            else:
                final.append((sent, sep))

        if final:
            final[-1] = (final[-1][0], "")
        return final

    def _preprocess(self, text: str) -> str:
        """Ön işleme: yalnız YATAY boşluk sadeleşir; satır sonları korunur.

        2026-09-29 (F3-14 / K5-2): eskiden ``\\s+`` -> ' ' satır sonlarını da
        siliyordu (ikinci satır bu yüzden ölüydü): "% yorum" satırından sonraki
        metin yorum satırına katılıyordu.
        """
        # Satır sonu dışındaki boşluk dizileri tek boşluğa
        text = re.sub(r'[^\S\n]+', ' ', text)
        # Satır sonu çevresindeki boşluklar atılır
        text = re.sub(r' *\n *', '\n', text)
        # Boş satır dizileri tek paragraf arasına
        text = re.sub(r'\n{2,}', '\n\n', text)
        return text.strip()

    def _protect_abbreviations(self, text: str) -> Tuple[str, dict]:
        """Kısaltmaları koru"""
        abbrev_map = {}
        counter = [0]

        def replacer(match):
            key = f"__ABBR_{counter[0]}__"
            abbrev_map[key] = match.group(0)
            counter[0] += 1
            return key

        text = self.abbrev_regex.sub(replacer, text)
        return text, abbrev_map

    def _protect_equation_refs(self, text: str) -> Tuple[str, dict]:
        """Denklem ve şekil referanslarını koru"""
        eq_map = {}
        counter = [0]

        def replacer(match):
            key = f"__EQREF_{counter[0]}__"
            eq_map[key] = match.group(0)
            counter[0] += 1
            return key

        text = self.EQUATION_REFS.sub(replacer, text)
        return text, eq_map

    def _protect_decimals(self, text: str) -> Tuple[str, dict]:
        """Ondalık sayıları koru"""
        decimal_map = {}
        counter = [0]

        def replacer(match):
            key = f"__DEC_{counter[0]}__"
            decimal_map[key] = match.group(0)
            counter[0] += 1
            return key

        # CORE-B15: Once cok parcali version numaralarini koru (3.14.2, v1.2.3)
        version_pattern = re.compile(r'\bv?\d+(?:\.\d+){2,}\b')
        text = version_pattern.sub(replacer, text)

        # Sonra normal ondalik (3.14)
        decimal_pattern = re.compile(r'\b\d+\.\d+\b')
        text = decimal_pattern.sub(replacer, text)
        return text, decimal_map

    def _protect_parentheses(self, text: str) -> Tuple[str, dict]:
        """Parantez içlerini koru (cümle sonu noktalama içeriyorsa)"""
        paren_map = {}
        counter = [0]

        # İç içe olmayan parantezler
        paren_pattern = re.compile(r'\([^()]*[.!?][^()]*\)')

        def replacer(match):
            key = f"__PAREN_{counter[0]}__"
            paren_map[key] = match.group(0)
            counter[0] += 1
            return key

        # Birkaç geçiş yap (iç içe parantezler için)
        for _ in range(3):
            new_text = paren_pattern.sub(replacer, text)
            if new_text == text:
                break
            text = new_text

        return text, paren_map

    def _restore_map(self, text: str, mapping: dict) -> str:
        """Placeholder'ları geri yükle"""
        for key, value in mapping.items():
            text = text.replace(key, value)
        return text

    def _do_split(self, text: str) -> List[Tuple[str, str]]:
        """Ana bölme işlemi: ``[(cümle, ardından gelen ayraç), ...]``."""
        # Önce paragrafları koru (ayraç '\n\n')
        paragraphs = [p for p in text.split('\n\n') if p.strip()]

        all_sentences: List[Tuple[str, str]] = []
        for p_idx, para in enumerate(paragraphs):
            # Cümle sonlarından böl: [cümle, ayraç, cümle, ayraç, ..., cümle]
            parts = self.sentence_end.split(para)
            para_sep = '\n\n' if p_idx < len(paragraphs) - 1 else ''
            for k in range(0, len(parts), 2):
                sent = parts[k].strip()
                sep = parts[k + 1] if k + 1 < len(parts) else para_sep
                if sent:
                    all_sentences.append((sent, sep))
                elif all_sentences and not all_sentences[-1][1]:
                    all_sentences[-1] = (all_sentences[-1][0], sep)

        return all_sentences

    # Uzun cümle bölme noktaları: noktalı virgül sonrası VEYA virgül+bağlaç.
    # Eski desende lookahead'den SONRA ikinci bir \s+ vardı; lookahead 'a'
    # harfinin üzerinde durduğu için o dal hiçbir girdide eşleşemiyordu
    # (yalnız ';' bölmesi çalışıyordu). \b, 'android' gibi kelimelerde
    # yanlış pozitifi önler.
    # 2026-09-29: ayraç yakalanır (grup) — birleştirme özgün boşluğu korur.
    _LONG_SPLIT_RE = re.compile(
        r'((?<=[;])\s+|(?<=,)\s+(?=(?:and|or|but|however|therefore|thus)\b))',
        re.IGNORECASE,
    )

    def _split_long_sentence(self, sentence: str) -> List[str]:
        """Çok uzun cümleleri alt-cümlelere böl"""
        return [part for part, _sep in self._split_long_pairs(sentence)]

    def _split_long_pairs(self, sentence: str) -> List[Tuple[str, str]]:
        """Uzun cümle -> ``[(alt cümle, ardından gelen ayraç), ...]`` (son ayraç "")."""
        # Noktalı virgül, virgül+bağlaç ile böl: [parça, ayraç, parça, ...]
        parts = self._LONG_SPLIT_RE.split(sentence)

        result: List[Tuple[str, str]] = []
        current = ""
        current_sep = ""   # current'tan sonra gelen ayraç (henüz eklenmedi)

        for k in range(0, len(parts), 2):
            part = parts[k]
            sep = parts[k + 1] if k + 1 < len(parts) else ""
            if not current:
                current = part
            elif len(current) + len(part) <= self.max_length:
                current = current + (current_sep or " ") + part
            else:
                result.append((current, current_sep or " "))
                current = part
            current_sep = sep

        if current:
            result.append((current, ""))

        return result if result else [(sentence, "")]

    def split_with_positions(self, text: str) -> List[dict]:
        """
        Pozisyon bilgisiyle birlikte böl

        Returns:
            [{"text": "...", "start": 0, "end": 50}, ...]
        """
        sentences = self.split(text)
        result = []

        pos = 0
        for sent in sentences:
            # Orijinal metinde bul
            start = text.find(sent, pos)
            if start == -1:
                # Tam eşleşme bulunamazsa yaklaşık pozisyon
                start = pos

            end = start + len(sent)
            result.append({
                "text": sent,
                "start": start,
                "end": end
            })
            pos = end

        return result


# Test
if __name__ == "__main__":
    splitter = SentenceSplitter()

    test_texts = [
        # Kısaltmalar
        "Dr. Smith studied the phenomenon. Prof. Johnson confirmed the results.",

        # Denklem referansları
        "As shown in Eq. 5, the force equals mass times acceleration. See also Fig. 3 for details.",

        # Ondalık sayılar
        "The value is 3.14159. The error margin is 0.05%.",

        # Parantez içi açıklamalar
        "The experiment (conducted by Dr. Smith et al.) showed significant results.",

        # Karışık
        """
        Newton's second law states that F = ma (see Eq. 1).
        Dr. Einstein later showed that E = mc².
        This was confirmed in Fig. 3 and Table 2.
        The measured value was 9.81 m/s² with an error of 0.02%.
        """,
    ]

    for i, text in enumerate(test_texts):
        print(f"\n{'='*50}")
        print(f"Test {i+1}:")
        print(f"Girdi: {text.strip()}")
        print(f"\nCümleler:")
        for j, sent in enumerate(splitter.split(text)):
            print(f"  {j+1}. {sent}")
