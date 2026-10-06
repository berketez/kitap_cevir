"""
Dictionary Loader
Büyük İngilizce-Türkçe sözlükten kategori bazlı terim yükleme
"""

import json
import os
from pathlib import Path
from typing import List, Dict, Set, Optional


class DictionaryLoader:
    """
    1.46M kayıtlık sözlükten kategori bazlı terim yükleme

    Kategoriler:
    - Technical (136K), Computer (34K), Math (4.6K)
    - Medical (52K), Chemistry (11K), Biology (5K)
    - Trade/Economic (65K), Law (32K)
    - Psychology (7K), Aeronautic (5.6K)
    - ve daha fazlası...
    """

    # Kitap türüne göre önerilen kategoriler
    BOOK_TYPE_CATEGORIES = {
        # Teknik & Mühendislik
        "technical": ["Technical", "Computer", "Math", "Informatics", "Electric"],
        "engineering": ["Technical", "Construction", "Aeronautic", "Automotive", "Marine", "Mechanic", "Mining"],
        "computer": ["Computer", "Informatics", "Technical", "Telecom", "Television", "Media"],

        # Bilim
        "science": ["Physics", "Chemistry", "Biology", "Math", "Geology", "Astronomy", "Statistics", "Biochemistry"],
        "medical": ["Medical", "Anatomy", "Pathology", "Physiology", "Dentistry", "Pharmaceutics", "Psychology", "Mental Health"],
        "nature": ["Botanic", "Zoology", "Marine Biology", "Environment", "Agriculture", "Forestry", "Ornithology", "Entomology"],

        # Sosyal Bilimler
        "psychology": ["Psychology", "Mental Health", "Social Sciences"],
        "philosophy": ["Philosophy", "Logic", "Religious"],
        "history": ["History", "Archaeology", "Archaic", "Ottoman Turkish", "Mythology"],
        "politics": ["Politics", "Social Sciences", "Military"],
        "law": ["Law", "Politics"],
        "business": ["Trade/Economic", "Law", "Insurance", "Advertising", "Industry"],

        # Sanat & Edebiyat
        "literature": ["Literature", "Linguistics", "Idioms", "Phrases", "Proverb", "Poetic/Literary"],
        "art": ["Art", "Music", "Cinema", "Theatre", "Photography", "Painting", "Architecture"],

        # Yaşam
        "sports": ["Sport", "Football", "Basketball", "Tennis", "Boxing", "Baseball", "Volleyball", "Chess"],
        "food": ["Gastronomy", "Food Engineering", "Agriculture"],
        "travel": ["Tourism", "Geography", "Places", "Transportation"],
        "military": ["Military", "Aeronautic", "Marine"],

        # Genel
        "general": ["General", "Common Usage", "Colloquial", "Speaking"],
    }

    def __init__(self, dictionary_path: str = None):
        if dictionary_path is None:
            dictionary_path = Path(__file__).parent.parent / "data" / "dictionary" / "dictionary.json"

        self.dictionary_path = Path(dictionary_path)
        self._data = None
        self._by_category = {}
        self._by_word = {}

    def _load(self):
        """Sözlüğü yükle (lazy loading)"""
        if self._data is not None:
            return

        if not self.dictionary_path.exists():
            print(f"[Dictionary] Sözlük bulunamadı: {self.dictionary_path}")
            self._data = []
            return

        # Dosya boyutu kontrolü (güvenlik)
        MAX_DICT_SIZE = 200 * 1024 * 1024  # 200MB
        file_size = os.path.getsize(self.dictionary_path)
        if file_size > MAX_DICT_SIZE:
            raise ValueError(f"dictionary.json too large: {file_size} bytes (max {MAX_DICT_SIZE})")

        print(f"[Dictionary] Sözlük yükleniyor: {self.dictionary_path}")
        with open(self.dictionary_path, 'r', encoding='utf-8') as f:
            self._data = json.load(f)

        print(f"[Dictionary] {len(self._data):,} kayıt yüklendi")

        # Kategori bazlı indeks oluştur
        for item in self._data:
            cat = item.get('category', 'General')
            if cat not in self._by_category:
                self._by_category[cat] = []
            self._by_category[cat].append(item)

            # Kelime bazlı indeks
            word = item.get('word', '').lower()
            if word:
                if word not in self._by_word:
                    self._by_word[word] = []
                self._by_word[word].append(item)

    def get_translation(self, word: str) -> Optional[str]:
        """
        Tek kelime için çeviri getir
        
        Args:
            word: İngilizce kelime
            
        Returns:
            Türkçe çeviri veya None
        """
        self._load()

        word_lower = word.lower()
        items = self._by_word.get(word_lower)
        if not items:
            return None

        # DATA-08: Kor `items[0]` yerine BARIZ dejenere kayitlari atla.
        # Kayit sirasi anlamsal oncelik degil (1.46M kayitli sozlukte 'algorithm'
        # icin ilk kayit General:'arap rakamlari sistemi' cikabiliyor) ama en
        # azindan bos tr ve EN==TR kimlik kayitlarini (sozlukte ~4883 adet)
        # atlayarak yanlis-pozitifleri azalt. Not: bu fallback core ceviri
        # yolunda artik cagrilmiyor (CORE-02 ile pozisyon/fuzzy sozluk enjeksiyonu
        # kaldirildi); yalnizca dis cagiranlar icin savunma.
        for item in items:
            tr = (item.get('tr') or '').strip()
            if tr and tr.lower() != word_lower:
                return tr

        # Hepsi dejenere ise ilk (varsa) tr'yi don
        first_tr = (items[0].get('tr') or '').strip()
        return first_tr or None

    def get_categories(self) -> List[str]:
        """Tüm kategorileri listele"""
        self._load()
        return list(self._by_category.keys())

    def get_category_count(self, category: str) -> int:
        """Kategorideki kayıt sayısı"""
        self._load()
        return len(self._by_category.get(category, []))

    def get_terms_by_category(self, category: str, limit: int = None) -> List[Dict]:
        """Kategorideki terimleri getir"""
        self._load()
        terms = self._by_category.get(category, [])
        if limit:
            return terms[:limit]
        return terms

    def get_terms_by_categories(self, categories: List[str]) -> Dict[str, str]:
        """
        Birden fazla kategoriden terim al

        Returns:
            {english_word: turkish_translation}
        """
        self._load()

        result = {}
        seen_words = set()

        for category in categories:
            for item in self._by_category.get(category, []):
                word = item.get('word', '').lower()
                tr = item.get('tr', '')

                # Sadece ilk çeviriyi al (duplikasyonları önle)
                if word and tr and word not in seen_words:
                    result[word] = tr
                    seen_words.add(word)

        return result

    def get_terms_for_book_type(self, book_type: str) -> Dict[str, str]:
        """
        Kitap türüne göre önerilen kategorilerden terim al

        Args:
            book_type: technical, engineering, medical, science, business, computer, psychology, general

        Returns:
            {english_word: turkish_translation}
        """
        categories = self.BOOK_TYPE_CATEGORIES.get(book_type, ["General"])
        return self.get_terms_by_categories(categories)

    def lookup(self, word: str) -> List[Dict]:
        """Kelime ara"""
        self._load()
        return self._by_word.get(word.lower(), [])

    def export_glossary(self, categories: List[str], output_path: str,
                       min_word_length: int = 3, exclude_common: bool = True) -> int:
        """
        Seçili kategorileri glossary JSON olarak export et

        Args:
            categories: Kategori listesi
            output_path: Çıktı dosya yolu
            min_word_length: Minimum kelime uzunluğu
            exclude_common: Çok yaygın kelimeleri hariç tut

        Returns:
            Export edilen terim sayısı
        """
        # Yaygın kelimeler (çeviriye gerek yok)
        common_words = {
            'the', 'a', 'an', 'is', 'are', 'was', 'were', 'be', 'been',
            'have', 'has', 'had', 'do', 'does', 'did', 'will', 'would',
            'could', 'should', 'may', 'might', 'must', 'can', 'and', 'or',
            'but', 'if', 'then', 'else', 'when', 'where', 'which', 'who',
            'what', 'how', 'why', 'this', 'that', 'these', 'those', 'it',
            'its', 'my', 'your', 'his', 'her', 'their', 'our', 'to', 'of',
            'in', 'on', 'at', 'for', 'with', 'by', 'from', 'up', 'down',
            'out', 'into', 'over', 'under', 'again', 'further', 'once',
        }

        terms = self.get_terms_by_categories(categories)

        # Filtrele
        glossary = {}
        for word, tr in terms.items():
            # Minimum uzunluk
            if len(word) < min_word_length:
                continue

            # Yaygın kelimeler
            if exclude_common and word in common_words:
                continue

            # Sadece harf içerenler
            if not word.replace('-', '').replace(' ', '').isalpha():
                continue

            glossary[word] = {
                "en": word,
                "tr": tr,
                "source": "dictionary"
            }

        # Kaydet
        with open(output_path, 'w', encoding='utf-8') as f:
            json.dump(glossary, f, ensure_ascii=False, indent=2)

        print(f"[Dictionary] {len(glossary):,} terim export edildi: {output_path}")
        return len(glossary)


# Test
if __name__ == "__main__":
    loader = DictionaryLoader()

    print("\nKategoriler:")
    for cat in sorted(loader.get_categories()):
        count = loader.get_category_count(cat)
        print(f"  {cat}: {count:,}")

    print("\n'algorithm' araması:")
    for item in loader.lookup("algorithm"):
        print(f"  [{item['category']}] {item['word']} -> {item['tr']}")

    print("\nTeknik kitap için terimler:")
    terms = loader.get_terms_for_book_type("technical")
    print(f"  {len(terms):,} terim")

    # İlk 10 örnek
    for word, tr in list(terms.items())[:10]:
        print(f"  {word} -> {tr}")
