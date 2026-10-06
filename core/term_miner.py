"""
Term Miner - Kitaptan otomatik terim çıkarma

Strateji:
1. Sıkı filtreler ile sadece gerçek terimleri yakala
2. Limit yok - filtreler kaliteli olduğu için gereksiz
3. Varsayılan: keep_original=True (İngilizce kalsın)
4. Curated glossary override edebilir
"""

import re
from collections import Counter
from typing import List, Dict, Set, Tuple
from dataclasses import dataclass


# İngilizce stopwords - bunlar terim olamaz
STOPWORDS = {
    'a', 'an', 'the', 'and', 'or', 'but', 'if', 'then', 'else', 'when',
    'at', 'by', 'for', 'with', 'about', 'against', 'between', 'into',
    'through', 'during', 'before', 'after', 'above', 'below', 'to',
    'from', 'up', 'down', 'in', 'out', 'on', 'off', 'over', 'under',
    'again', 'further', 'then', 'once', 'here', 'there', 'where', 'why',
    'how', 'all', 'each', 'few', 'more', 'most', 'other', 'some', 'such',
    'no', 'nor', 'not', 'only', 'own', 'same', 'so', 'than', 'too',
    'very', 's', 't', 'can', 'will', 'just', 'don', 'should', 'now',
    'i', 'me', 'my', 'myself', 'we', 'our', 'ours', 'ourselves', 'you',
    'your', 'yours', 'yourself', 'yourselves', 'he', 'him', 'his',
    'himself', 'she', 'her', 'hers', 'herself', 'it', 'its', 'itself',
    'they', 'them', 'their', 'theirs', 'themselves', 'what', 'which',
    'who', 'whom', 'this', 'that', 'these', 'those', 'am', 'is', 'are',
    'was', 'were', 'be', 'been', 'being', 'have', 'has', 'had', 'having',
    'do', 'does', 'did', 'doing', 'would', 'could', 'ought', 'i\'m',
    'you\'re', 'he\'s', 'she\'s', 'it\'s', 'we\'re', 'they\'re', 'i\'ve',
    'you\'ve', 'we\'ve', 'they\'ve', 'i\'d', 'you\'d', 'he\'d', 'she\'d',
    'we\'d', 'they\'d', 'i\'ll', 'you\'ll', 'he\'ll', 'she\'ll', 'we\'ll',
    'they\'ll', 'isn\'t', 'aren\'t', 'wasn\'t', 'weren\'t', 'hasn\'t',
    'haven\'t', 'hadn\'t', 'doesn\'t', 'don\'t', 'didn\'t', 'won\'t',
    'wouldn\'t', 'shan\'t', 'shouldn\'t', 'can\'t', 'cannot', 'couldn\'t',
    'mustn\'t', 'let\'s', 'that\'s', 'who\'s', 'what\'s', 'here\'s',
    'there\'s', 'when\'s', 'where\'s', 'why\'s', 'how\'s', 'because',
    'as', 'until', 'while', 'of', 'both', 'either', 'neither', 'also',
    'however', 'although', 'though', 'even', 'still', 'already', 'yet',
    'ever', 'never', 'always', 'often', 'sometimes', 'usually', 'rather',
    'quite', 'almost', 'enough', 'much', 'many', 'several', 'any',
    'every', 'another', 'one', 'two', 'three', 'four', 'five', 'first',
    'second', 'third', 'last', 'next', 'new', 'old', 'good', 'bad',
    'great', 'little', 'big', 'small', 'long', 'short', 'high', 'low',
    'right', 'left', 'back', 'well', 'way', 'part', 'place', 'case',
    'week', 'day', 'year', 'time', 'thing', 'fact', 'said', 'says',
    'make', 'made', 'get', 'got', 'go', 'went', 'come', 'came', 'take',
    'took', 'see', 'saw', 'know', 'knew', 'think', 'thought', 'look',
    'looked', 'want', 'give', 'use', 'find', 'tell', 'ask', 'work',
    'seem', 'feel', 'try', 'leave', 'call', 'need', 'may', 'must',
    'shall', 'like', 'just', 'going', 'people', 'person', 'man', 'men',
    'woman', 'women', 'child', 'children', 'world', 'life', 'hand',
    'yes', 'no', 'maybe', 'perhaps', 'really', 'actually', 'certainly',
    # Ekstra yaygın kelimeler
    'something', 'anything', 'nothing', 'everything', 'someone', 'anyone',
    'everyone', 'nobody', 'somewhere', 'anywhere', 'everywhere', 'nowhere',
    'chapter', 'book', 'page', 'section', 'part', 'introduction', 'conclusion'
}

# Yaygın kelimeler - tek başına terim olamaz ama phrase içinde olabilir
COMMON_WORDS = {
    'the', 'of', 'and', 'a', 'to', 'in', 'is', 'it', 'for', 'that',
    'was', 'on', 'are', 'as', 'with', 'his', 'they', 'be', 'at', 'one',
    'have', 'this', 'from', 'by', 'hot', 'word', 'but', 'what', 'some',
    'we', 'can', 'out', 'other', 'were', 'all', 'there', 'when', 'up',
    'use', 'your', 'how', 'said', 'an', 'each', 'she', 'which', 'do',
    'their', 'time', 'if', 'will', 'way', 'about', 'many', 'then'
}


@dataclass
class ExtractedTerm:
    """Çıkarılan terim bilgisi"""
    term: str
    frequency: int
    pattern_type: str  # multi_word, title_case, all_caps, hyphenated
    keep_original: bool = True


class TermMiner:
    """Kitaptan otomatik terim çıkarma"""

    def __init__(self,
                 min_freq: int = 2,
                 min_length: int = 3,
                 exclude_stopwords: bool = True):
        self.min_freq = min_freq
        self.min_length = min_length
        self.exclude_stopwords = exclude_stopwords

    def extract_terms(self, text: str) -> List[ExtractedTerm]:
        """
        Metinden terimleri çıkar

        Returns:
            Frekansa göre sıralı terim listesi
        """
        all_terms = []

        # 1. Multi-word phrases (2-4 kelime, Title Case)
        multi_word = self._extract_multi_word_phrases(text)
        all_terms.extend(multi_word)

        # 2. Title Case single words (özel isimler, konseptler)
        title_case = self._extract_title_case_words(text)
        all_terms.extend(title_case)

        # 3. ALL CAPS (kısaltmalar)
        all_caps = self._extract_all_caps(text)
        all_terms.extend(all_caps)

        # 4. Hyphenated terms
        hyphenated = self._extract_hyphenated(text)
        all_terms.extend(hyphenated)

        # Birleştir ve deduplicate
        term_dict = {}
        for term in all_terms:
            key = term.term.lower()
            if key in term_dict:
                # Frekansı topla, en yüksek öncelikli pattern'ı tut
                existing = term_dict[key]
                existing.frequency += term.frequency
            else:
                term_dict[key] = term

        # Filtrele ve sırala
        result = []
        for term in term_dict.values():
            if self._is_valid_term(term):
                result.append(term)

        # Frekansa göre sırala (yüksekten düşüğe)
        result.sort(key=lambda x: x.frequency, reverse=True)

        return result

    def _extract_multi_word_phrases(self, text: str) -> List[ExtractedTerm]:
        """2-4 kelimelik Title Case phrase'leri çıkar"""
        # Pattern: 2-4 consecutive Title Case words
        pattern = r'\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+){1,3})\b'
        matches = re.findall(pattern, text)

        counter = Counter(matches)
        terms = []

        for phrase, freq in counter.items():
            if freq >= self.min_freq:
                # Stopword-only phrase'leri atla
                words = phrase.lower().split()
                if not all(w in STOPWORDS for w in words):
                    terms.append(ExtractedTerm(
                        term=phrase,
                        frequency=freq,
                        pattern_type="multi_word"
                    ))

        return terms

    def _extract_title_case_words(self, text: str) -> List[ExtractedTerm]:
        """Cümle başı olmayan Title Case kelimeleri çıkar"""
        # Cümle başlarını hariç tut
        # Pattern: Title Case word not after . ! ? or start
        pattern = r'(?<![.!?\n])\s+([A-Z][a-z]{2,})\b'
        matches = re.findall(pattern, text)

        counter = Counter(matches)
        terms = []

        for word, freq in counter.items():
            if freq >= self.min_freq and word.lower() not in STOPWORDS:
                terms.append(ExtractedTerm(
                    term=word,
                    frequency=freq,
                    pattern_type="title_case"
                ))

        return terms

    def _extract_all_caps(self, text: str) -> List[ExtractedTerm]:
        """ALL CAPS kısaltmaları çıkar (2-10 karakter)"""
        pattern = r'\b([A-Z]{2,10})\b'
        matches = re.findall(pattern, text)

        counter = Counter(matches)
        terms = []

        for word, freq in counter.items():
            if freq >= self.min_freq:
                # Yaygın kısaltmaları hariç tut
                if word not in {'I', 'A', 'OK', 'AM', 'PM', 'TV', 'US', 'UK', 'EU'}:
                    terms.append(ExtractedTerm(
                        term=word,
                        frequency=freq,
                        pattern_type="all_caps"
                    ))

        return terms

    def _extract_hyphenated(self, text: str) -> List[ExtractedTerm]:
        """Tire ile bağlı terimleri çıkar"""
        pattern = r'\b([a-zA-Z]+-[a-zA-Z]+(?:-[a-zA-Z]+)?)\b'
        matches = re.findall(pattern, text)

        counter = Counter(matches)
        terms = []

        for word, freq in counter.items():
            if freq >= self.min_freq and len(word) >= self.min_length:
                terms.append(ExtractedTerm(
                    term=word,
                    frequency=freq,
                    pattern_type="hyphenated"
                ))

        return terms

    def _is_valid_term(self, term: ExtractedTerm) -> bool:
        """Terimin geçerli olup olmadığını kontrol et"""
        t = term.term

        # Minimum uzunluk
        if len(t) < self.min_length:
            return False

        # Minimum frekans
        if term.frequency < self.min_freq:
            return False

        # Stopword kontrolü (tek kelime için)
        if ' ' not in t and '-' not in t:
            if t.lower() in STOPWORDS:
                return False

        # Sadece sayı veya noktalama
        if re.match(r'^[\d\W]+$', t):
            return False

        return True

    def terms_to_glossary(self, terms: List[ExtractedTerm]) -> Dict:
        """Terimleri glossary formatına çevir"""
        glossary = {
            "description": "Auto-mined terms from book",
            "strategy": "hard",  # Koruma modu
            "terms": {},
            "do_not_translate": []
        }

        for term in terms:
            if term.keep_original:
                # do_not_translate listesine ekle
                glossary["do_not_translate"].append(term.term)
            else:
                # terms dict'ine ekle
                glossary["terms"][term.term.lower()] = {
                    "tr": term.term,  # Varsayılan: aynı
                    "context": f"Auto-detected ({term.pattern_type})",
                    "priority": "medium",
                    "frequency": term.frequency
                }

        return glossary


def mine_terms_from_text(text: str, min_freq: int = 2) -> List[ExtractedTerm]:
    """Convenience function"""
    miner = TermMiner(min_freq=min_freq)
    return miner.extract_terms(text)


def mine_terms_from_file(file_path: str, min_freq: int = 2) -> List[ExtractedTerm]:
    """Dosyadan terim çıkar"""
    with open(file_path, 'r', encoding='utf-8') as f:
        text = f.read()
    return mine_terms_from_text(text, min_freq)


# Test
if __name__ == "__main__":
    test_text = """
    The Convergent Nozzle accelerates subsonic flow toward the Nozzle Throat.
    Specific Impulse measures how efficiently a Rocket Engine uses propellant.
    The Convergent Nozzle and the Divergent Section form a de Laval nozzle.
    Chamber Pressure and Expansion Ratio set the exit velocity.
    Specific Impulse is quoted in seconds for most Rocket Engine designs.
    DNA and RNA are important in biology. Self-improvement is key.
    """

    miner = TermMiner(min_freq=1)  # Test için min_freq=1
    terms = miner.extract_terms(test_text)

    print(f"Bulunan terimler ({len(terms)}):")
    for t in terms[:20]:
        print(f"  {t.term}: {t.frequency}x ({t.pattern_type})")
