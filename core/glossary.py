"""
Terminoloji Yönetimi (Glossary Manager) - v2
WMT25 DuTerm yaklaşımı: Soft constraints + Curated termbase

Strateji:
- "off": Glossary kapalı
- "soft": Terimler hint olarak verilir, model karar verir
- "hard": Placeholder ile zorla (eski yöntem, önerilmez)

Kaynaklar:
- WMT25 DuTerm: https://arxiv.org/html/2511.07461
- EAMT 2023: https://aclanthology.org/2023.eamt-1.34/

KARAR 25 (5 Ekim 2026): glossary/ altında yalnız elle doğrulanmış
curated.json ve do_not_translate.json kaldı; genel alan sözlükleri silindi.
Kitaba özgü terimler latex_pipeline.kitap_sozlugu (glossary_shared.tsv)
ile yönetilir. Bu sınıfın kamu API'si geriye uyumludur.
"""

import json
import os
import re
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from rapidfuzz import fuzz, process

# Logger import
try:
    from core.logger import logger
except ImportError:
    logger = None


class GlossaryManager:
    """
    Terminoloji sözlüğü yöneticisi - v2 (WMT25 DuTerm yaklaşımı)

    Yeni Özellikler:
    - Soft/Hard mod desteği
    - Segment başına terim limiti
    - Curated glossary öncelikli
    - Multi-word term odaklı
    """

    # Glossary modu
    MODE_OFF = "off"
    MODE_SOFT = "soft"
    MODE_HARD = "hard"
    MODE_HYBRID = "hybrid"  # Codex önerisi: curated=hard, diğer=soft
    MODE_POST = "post"  # Çeviri sırasında müdahale yok, sonunda post-process ile düzelt
    
    # Çevrilmemesi gereken çok yaygın kelimeler (model zaten doğru çevirir)
    COMMON_WORDS_TO_SKIP = {
        # Zamirler
        'i', 'you', 'he', 'she', 'it', 'we', 'they', 'me', 'him', 'her', 'us', 'them',
        'my', 'your', 'his', 'its', 'our', 'their', 'mine', 'yours', 'ours', 'theirs',
        # Bağlaçlar
        'and', 'or', 'but', 'so', 'yet', 'for', 'nor',
        # Edatlar
        'in', 'on', 'at', 'to', 'from', 'with', 'by', 'about', 'into', 'through',
        'during', 'before', 'after', 'above', 'below', 'between', 'under', 'over',
        # Artikeller ve belirteçler
        'a', 'an', 'the', 'this', 'that', 'these', 'those', 'some', 'any', 'no',
        # Yardımcı fiiller
        'is', 'are', 'was', 'were', 'be', 'been', 'being', 'have', 'has', 'had',
        'do', 'does', 'did', 'will', 'would', 'could', 'should', 'may', 'might',
        'must', 'shall', 'can',
        # Çok yaygın kelimeler
        'all', 'each', 'every', 'both', 'few', 'more', 'most', 'other', 'another',
        'such', 'what', 'which', 'who', 'whom', 'whose', 'how', 'when', 'where', 'why',
        'just', 'only', 'also', 'very', 'too', 'even', 'still', 'already', 'always',
        'never', 'ever', 'often', 'sometimes', 'usually', 'really', 'quite',
        'here', 'there', 'now', 'then', 'today', 'tomorrow', 'yesterday',
        'new', 'old', 'good', 'bad', 'great', 'little', 'big', 'small', 'long', 'short',
        'first', 'last', 'next', 'many', 'much', 'less', 'least', 'more', 'most',
        'like', 'get', 'got', 'make', 'made', 'go', 'went', 'gone', 'come', 'came',
        'take', 'took', 'give', 'gave', 'find', 'found', 'know', 'knew', 'think',
        'see', 'saw', 'want', 'need', 'use', 'try', 'ask', 'tell', 'told', 'say', 'said',
        'work', 'feel', 'felt', 'become', 'leave', 'left', 'put', 'keep', 'let', 'begin',
        'seem', 'help', 'show', 'hear', 'play', 'run', 'move', 'live', 'believe',
        'book', 'man', 'men', 'woman', 'women', 'day', 'year', 'time', 'way', 'life',
        'world', 'hand', 'part', 'place', 'case', 'week', 'company', 'system', 'program',
        'question', 'work', 'government', 'number', 'night', 'point', 'home', 'water',
        'room', 'mother', 'area', 'money', 'story', 'fact', 'month', 'lot', 'right',
        'study', 'word', 'business', 'issue', 'side', 'kind', 'head', 'house', 'service',
        'friend', 'father', 'power', 'hour', 'game', 'line', 'end', 'member', 'law',
        'car', 'city', 'community', 'name', 'president', 'team', 'minute', 'idea',
        'kid', 'body', 'information', 'back', 'parent', 'face', 'others', 'level',
        'office', 'door', 'health', 'person', 'art', 'war', 'history', 'party',
        'result', 'change', 'morning', 'reason', 'research', 'girl', 'guy', 'moment',
        'air', 'teacher', 'force', 'education',
    }

    # Bilimsel/teknik kategoriler. KARAR 25 (2026-10-05): bu kategorilerde
    # eskiden yüklenen genel science_engineering.json silindi; ad geriye
    # uyum için durur (dış çağıran okuyabilir).
    SCIENTIFIC_CATEGORIES = {"science", "engineering", "technical", "computer", "medical"}

    def __init__(self, glossary_dir: str = None, mode: str = "soft",
                 max_terms_per_segment: int = 5, use_curated: bool = True,
                 book_type: str = "general"):
        """
        Args:
            glossary_dir: Glossary dizini
            mode: "off", "soft", "hard"
            max_terms_per_segment: Segment başına max terim (fazlası = devre dışı)
            use_curated: True = curated.json, False = tüm glossary dosyaları
            book_type: Kitap turu (general, science, engineering, technical, vb.)
        """
        self.glossary_dir = glossary_dir or self._get_default_dir()
        self.mode = mode
        self.max_terms_per_segment = max_terms_per_segment
        self.use_curated = use_curated
        self.book_type = book_type

        self.terms: Dict[str, dict] = {}
        self.term_patterns: List[Tuple[re.Pattern, str, dict]] = []
        self._first_occurrence_used: set = set()
        self.do_not_translate: List[str] = []  # Çevrilmeyecek terimler

        # Mode off değilse yükle
        if self.mode != self.MODE_OFF:
            self._load_glossaries()

    def _get_default_dir(self) -> str:
        """Varsayılan glossary dizinini bul"""
        current = Path(__file__).parent.parent
        return str(current / "glossary")

    def _load_glossaries(self):
        """Yeni strateji: Curated veya tüm glossary dosyalarını yükle"""
        glossary_path = Path(self.glossary_dir)

        if not glossary_path.exists():
            print(f"[Glossary] Dizin bulunamadı: {glossary_path}")
            return

        if self.use_curated:
            # Sadece curated.json yükle
            curated_path = glossary_path / "curated.json"
            if curated_path.exists():
                self._load_curated_glossary(curated_path)
                # print(f"[Glossary] Curated glossary yüklendi: {len(self.terms)} terim")
            else:
                pass  # curated.json bulunamadı - sessiz
                # Fallback: do_not_translate.json varsa onu yükle
                dnt_path = glossary_path / "do_not_translate.json"
                if dnt_path.exists():
                    self._load_curated_glossary(dnt_path)

            # KARAR 25 (2026-10-05): bilimsel/teknik kitaplar için yüklenen
            # genel science_engineering.json SİLİNDİ — sabit genel sözlük yok;
            # her kitap kendi sözlüğünü çıkarır (latex_pipeline.kitap_sozlugu,
            # glossary_shared.tsv). SCIENTIFIC_CATEGORIES geriye uyum için durur.
        else:
            # Eski davranış: tüm JSON dosyalarını yükle (ÖNERİLMEZ)
            for json_file in glossary_path.glob("*.json"):
                if json_file.stem != "technical":  # technical.json çok büyük, atla
                    self._load_glossary_file(json_file)

        # Pattern'ları oluştur
        self._build_patterns()

    def _load_curated_glossary(self, file_path: Path):
        """Curated glossary yükle - yeni format"""
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                data = json.load(f)

            # Terimler
            if "terms" in data:
                for term_en, term_data in data["terms"].items():
                    term_lower = term_en.lower()

                    # Tek kelimelik yaygın kelimeler atla (soft constraint)
                    if ' ' not in term_en and term_lower in self.COMMON_WORDS_TO_SKIP:
                        continue

                    self.terms[term_lower] = {
                        "en": term_en,
                        "tr": term_data.get("tr", term_en),
                        "keep_original": term_data.get("keep_original", False),
                        "context": term_data.get("context", ""),
                        "priority": term_data.get("priority", "medium"),
                        "source": "curated"
                    }

            # Çevrilmeyecek terimler
            if "do_not_translate" in data:
                self.do_not_translate = data["do_not_translate"]
                # do_not_translate listesini de terms'e ekle
                for term in self.do_not_translate:
                    term_lower = term.lower()
                    if term_lower not in self.terms:
                        self.terms[term_lower] = {
                            "en": term,
                            "tr": term,  # Aynı kal
                            "keep_original": True,
                            "context": "do_not_translate",
                            "priority": "high",
                            "source": "curated"
                        }

            # Sessiz yükleme
            pass

        except Exception as e:
            error_msg = f"Glossary yükleme hatası ({file_path.name}): {e}"
            if logger:
                logger.glossary(error_msg, "ERROR")

    def _load_glossary_file(self, file_path: Path):
        """Eski format glossary yükle (geriye uyumluluk)"""
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                data = json.load(f)

            if "terms" in data:
                for term_en, term_data in data["terms"].items():
                    term_lower = term_en.lower()

                    # Tek kelimelik yaygın kelimeler atla
                    if ' ' not in term_en and term_lower in self.COMMON_WORDS_TO_SKIP:
                        continue

                    # 3 harften kısa atla
                    if len(term_en) < 4 and ' ' not in term_en:
                        continue

                    if term_lower not in self.terms:
                        self.terms[term_lower] = {
                            "en": term_en,
                            "tr": term_data.get("tr", term_en),
                            "keep_original": term_data.get("keep_original", False),
                            "context": term_data.get("context", ""),
                            "note": term_data.get("note", ""),
                            "source": file_path.stem
                        }

                # Sessiz yükleme
                pass

        except Exception as e:
            error_msg = f"Glossary yükleme hatası ({file_path.name}): {e}"
            if logger:
                logger.glossary(error_msg, "ERROR")

    def _build_patterns(self):
        """Terim eşleştirme için hızlı lookup oluştur"""
        # Uzun terimler önce eşleşsin
        sorted_terms = sorted(self.terms.items(), key=lambda x: len(x[0]), reverse=True)

        # Eski yavaş yöntem (regex) - sadece çok kelimeli terimler için
        self.term_patterns = []
        
        # Yeni hızlı yöntem - tek kelimeli terimler için set lookup
        self.term_lookup = {}  # lowercase -> term_data
        self.multi_word_patterns = []  # Çok kelimeli terimler için regex
        
        for term_en, term_data in sorted_terms:
            # Çok kelimeli veya özel karakterli mi? (boşluk, tire, kesme işareti)
            if ' ' in term_en or '-' in term_en or "'" in term_en:
                # Regex gerekli (özel karakterler için)
                pattern = re.compile(
                    r'\b' + re.escape(term_en) + r'\b',
                    re.IGNORECASE
                )
                self.multi_word_patterns.append((pattern, term_en, term_data))
                self.term_patterns.append((pattern, term_en, term_data))
            else:
                # Saf tek kelime - set lookup kullan (çok hızlı)
                self.term_lookup[term_en.lower()] = term_data
                # Yine de patterns'a ekle (geriye uyumluluk)
                pattern = re.compile(
                    r'\b' + re.escape(term_en) + r'\b',
                    re.IGNORECASE
                )
                self.term_patterns.append((pattern, term_en, term_data))

    def add_term(self, term_en: str, term_tr: str, context: str = "",
                 keep_original: bool = False, note: str = ""):
        """Yeni terim ekle"""
        term_lower = term_en.lower()
        self.terms[term_lower] = {
            "en": term_en,
            "tr": term_tr,
            "keep_original": keep_original,
            "context": context,
            "note": note,
            "source": "custom"
        }
        self._build_patterns()

    def add_terms_from_dict(self, terms: Dict[str, str]):
        """Sözlükten toplu terim ekle"""
        for en, tr in terms.items():
            self.add_term(en, tr)

    def add_mined_terms(self, mined_terms: List, keep_original: bool = True):
        """
        Term miner'dan gelen terimleri ekle

        Args:
            mined_terms: TermMiner.extract_terms() çıktısı (ExtractedTerm listesi)
            keep_original: True = İngilizce kalsın, False = çevirmeye çalış
        """
        count = 0
        skipped = 0

        for term in mined_terms:
            term_lower = term.term.lower()

            # Zaten curated'da varsa atla (curated öncelikli)
            if term_lower in self.terms and self.terms[term_lower].get("source") == "curated":
                skipped += 1
                continue

            # Yaygın kelimeler atla
            if term_lower in self.COMMON_WORDS_TO_SKIP:
                continue

            # Ekle veya güncelle
            self.terms[term_lower] = {
                "en": term.term,
                "tr": term.term,  # keep_original=True ise aynı kalacak
                "keep_original": keep_original,
                "context": f"auto-mined ({term.pattern_type})",
                "frequency": term.frequency,
                "source": "mined"
            }
            count += 1

        # Pattern'ları yeniden oluştur
        self._build_patterns()

        print(f"[Glossary] {count} mined terim eklendi ({skipped} curated ile çakışan atlandı)")
        return count

    def load_from_dictionary(self, book_type: str = "general", categories: List[str] = None,
                             max_terms: int = 5000):
        """
        Büyük sözlükten kitap türüne göre terim yükle

        Args:
            book_type: technical, engineering, medical, science, business, computer, psychology, general
            categories: Özel kategori listesi (book_type yerine)
            max_terms: Maksimum terim sayısı (performans için)
        """
        try:
            from core.dictionary_loader import DictionaryLoader
        except ImportError:
            print("[Glossary] Dictionary loader bulunamadı")
            return 0

        loader = DictionaryLoader()

        if categories:
            terms = loader.get_terms_by_categories(categories)
        else:
            terms = loader.get_terms_for_book_type(book_type)

        # Maksimum terim sayısını sınırla
        count = 0
        skipped_common = 0
        for word, tr in terms.items():
            if count >= max_terms:
                break

            # Çok kısa kelimeleri atla (3 harften az)
            if len(word) < 3:
                continue
            
            # Çok yaygın kelimeleri atla (model zaten doğru çevirir)
            if word.lower() in self.COMMON_WORDS_TO_SKIP:
                skipped_common += 1
                continue

            # Zaten varsa atla
            if word.lower() in self.terms:
                continue

            self.terms[word.lower()] = {
                "en": word,
                "tr": tr,
                "keep_original": False,
                "context": "",
                "note": "",
                "source": "dictionary"
            }
            count += 1
        
        if skipped_common > 0 and logger:
            logger.glossary(f"{skipped_common} yaygın kelime atlandı", "DEBUG")

        # Pattern'ları yeniden oluştur
        self._build_patterns()

        # Log mesajı
        source_info = f"kategoriler: {categories}" if categories else f"kitap türü: {book_type}"
        print(f"[Glossary] Sözlükten {count:,} terim yüklendi ({source_info})")
        return count

    def protect_terms(self, text: str) -> Tuple[str, List[dict]]:
        """
        Metindeki teknik terimleri bul ve işle.

        Mod'a göre davranış:
        - "off": Hiçbir şey yapma
        - "soft": Terimleri bul ama değiştirme, sadece hint listesi döndür
        - "hard": Terimleri placeholder ile değiştir (eski davranış)
        - "hybrid": Curated terimler hard, diğerleri soft (Codex önerisi)

        Segment limit: max_terms_per_segment'i aşarsa devre dışı bırak

        Returns:
            (metin, bulunan terim listesi)
        """
        # Mode off ise hiçbir şey yapma
        if self.mode == self.MODE_OFF:
            return text, []

        # Mode post ise çeviri sırasında müdahale yok, sonunda post-process düzeltecek
        if self.mode == self.MODE_POST:
            return text, []

        protected = []
        result = text
        counter = [0]

        # None kontrolü
        if not text:
            return text, protected
        text_preview = text[:50].replace('\n', ' ') + "..." if len(text) > 50 else text.replace('\n', ' ')

        # Önce tüm terimleri bul (sayı için)
        found_terms = self._find_all_terms(text)

        # SEGMENT LIMIT kontrolü - limiti aşarsa en öncelikli terimleri tut
        if len(found_terms) > self.max_terms_per_segment:
            if logger:
                logger.glossary(
                    f"Segment limit aşıldı ({len(found_terms)} > {self.max_terms_per_segment}), "
                    f"en öncelikli {self.max_terms_per_segment} terim korunuyor: \"{text_preview}\"", "WARNING"
                )
            # Curated ve keep_original terimler önce, sonra pozisyona göre
            def term_priority(t):
                term_info = self.terms.get(t["en"].lower(), {})
                is_curated = term_info.get("source") == "curated"
                is_keep = t.get("keep_original", False)
                return (0 if is_curated or is_keep else 1, t["start"])
            found_terms.sort(key=term_priority)
            found_terms = found_terms[:self.max_terms_per_segment]
            found_terms.sort(key=lambda x: x["start"])  # Pozisyon sırasına geri döndür

        # if logger:
        #     logger.glossary(f"protect_terms: {len(found_terms)} terim bulundu, mod={self.mode}", "DEBUG")

        # SOFT mod: Terimleri değiştirme, sadece hint listesi döndür
        if self.mode == self.MODE_SOFT:
            for term_data in found_terms:
                protected.append({
                    "index": counter[0],
                    "original": term_data["match"],
                    "term_en": term_data["en"],
                    "term_tr": term_data["tr"],
                    "keep_original": term_data.get("keep_original", False),
                    "mode": "soft"  # İşaretçi: bu soft mod
                })
                counter[0] += 1
            # Metin değişmeden döner, sadece hint listesi var
            return text, protected

        # HYBRID mod: Curated=hard, diğer=soft (Codex önerisi)
        if self.mode == self.MODE_HYBRID:
            for term_data in found_terms:
                idx = counter[0]
                term_lower = term_data["en"].lower()
                term_info = self.terms.get(term_lower, {})
                is_curated = term_info.get("source") == "curated"
                is_keep_original = term_data.get("keep_original", False)

                # Curated veya keep_original terimler HARD mode
                if is_curated or is_keep_original:
                    protected.append({
                        "index": idx,
                        "original": term_data["match"],
                        "term_en": term_data["en"],
                        "term_tr": term_data["tr"],
                        "keep_original": is_keep_original,
                        "mode": "hard"
                    })
                    # Alfanumerik placeholder - neural MT modelleri XML'i bozuyor
                    pattern = re.compile(re.escape(term_data["match"]), re.IGNORECASE)
                    placeholder = f'@@TERM{idx:04d}@@'
                    result = pattern.sub(placeholder, result, count=1)
                else:
                    # Diğer terimler SOFT mode (hint olarak)
                    protected.append({
                        "index": idx,
                        "original": term_data["match"],
                        "term_en": term_data["en"],
                        "term_tr": term_data["tr"],
                        "keep_original": is_keep_original,
                        "mode": "soft"
                    })
                counter[0] += 1

            if logger:
                hard_count = sum(1 for p in protected if p["mode"] == "hard")
                soft_count = len(protected) - hard_count
                logger.glossary(f"protect_terms (hybrid): {hard_count} hard, {soft_count} soft", "DEBUG")

            return result, protected

        # HARD mod: Alfanumerik placeholder ile değiştir
        for term_data in found_terms:
            idx = counter[0]
            protected.append({
                "index": idx,
                "original": term_data["match"],
                "term_en": term_data["en"],
                "term_tr": term_data["tr"],
                "keep_original": term_data.get("keep_original", False),
                "mode": "hard"
            })

            # Alfanumerik placeholder - neural MT modelleri XML'i bozuyor
            pattern = re.compile(re.escape(term_data["match"]), re.IGNORECASE)
            placeholder = f'@@TERM{idx:04d}@@'
            result = pattern.sub(placeholder, result, count=1)
            counter[0] += 1

        if logger:
            logger.glossary(f"protect_terms bitti: {len(protected)} terim ({self.mode} mod)", "DEBUG")

        return result, protected

    def _find_all_terms(self, text: str) -> List[dict]:
        """Metindeki tüm terimleri bul (sayı ve liste için)"""
        found = []
        text_lower = text.lower()

        # Çok kelimeli terimler önce (uzun eşleşmeler öncelikli)
        for pattern, term_en, term_data in self.multi_word_patterns:
            matches = pattern.finditer(text)
            for match in matches:
                found.append({
                    "match": match.group(0),
                    "en": term_data["en"],
                    "tr": term_data["tr"],
                    "keep_original": term_data.get("keep_original", False),
                    "start": match.start(),
                    "end": match.end()
                })

        # Tek kelimeli terimler
        if hasattr(self, 'term_lookup') and self.term_lookup:
            word_pattern = re.compile(r'\b[a-zA-Z]{2,}\b')  # Min 2 karakter (CFD, DNS gibi kısaltmalar için)
            for match in word_pattern.finditer(text):
                word = match.group(0)
                word_lower = word.lower()

                # Zaten bulunan bir terimin parçası mı?
                is_part_of_found = any(
                    f["start"] <= match.start() < f["end"]
                    for f in found
                )
                if is_part_of_found:
                    continue

                data = self.term_lookup.get(word_lower)
                if data:
                    found.append({
                        "match": word,
                        "en": data["en"],
                        "tr": data["tr"],
                        "keep_original": data.get("keep_original", False),
                        "start": match.start(),
                        "end": match.end()
                    })

        # Pozisyona göre sırala
        found.sort(key=lambda x: x["start"])
        return found

    def restore_terms(self, text: str, protected: List[dict], use_turkish: bool = True) -> Tuple[str, List[int]]:
        """
        XML tag'leri terimlerle değiştir (Codex C çözümü: XML + post-processing fallback)

        Args:
            text: XML tag'li metin
            protected: protect_terms'den dönen liste
            use_turkish: True ise Türkçe, False ise orijinal terim kullan

        Returns:
            (restore edilmiş metin, bulunamayan index'ler listesi)
        """
        if logger:
            logger.glossary(f"restore_terms basladi: {len(protected)} terim geri yuklenecek", "DEBUG")
            text_preview = text[:100].replace('\n', ' ') + "..." if len(text) > 100 else text.replace('\n', ' ')
            logger.glossary(f"  Model ciktisi: \"{text_preview}\"", "DEBUG")

        result = text
        restored_count = 0
        not_found = []

        for item in protected:
            idx = item['index']
            term_en = item.get('term_en', '').lower()
            mode = item.get('mode', 'hard')

            # Soft mod terimler metin içinde değiştirilmedi, atla
            if mode == "soft":
                continue

            # Replacement belirleme
            if use_turkish:
                if item.get("keep_original", False):
                    replacement = item["original"]
                else:
                    replacement = self._match_case(item["original"], item["term_tr"])

                # İlk geçiş açıklaması
                term_data = self.terms.get(term_en, {})
                note = term_data.get("note", "")
                if note and term_en not in self._first_occurrence_used:
                    replacement = f"{replacement} ({note})"
                    self._first_occurrence_used.add(term_en)
                    if logger:
                        logger.glossary(f"  Ilk gecis aciklamasi: '{item['term_en']}' -> '{replacement}'", "INFO")
            else:
                replacement = item["original"]

            old_result = result

            # 1. Alfanumerik placeholder formatı (primary)
            idx_variants = [f"{idx:04d}", str(idx), f"{idx:02d}"]
            for idx_var in idx_variants:
                placeholder_patterns = [
                    f"@@TERM{idx_var}@@",
                    f"@@TERIM{idx_var}@@",
                    f"@@ TERM{idx_var} @@",
                    f"@@TERM {idx_var}@@",
                    f"[[TERM_{idx_var}]]",
                ]
                for pattern in placeholder_patterns:
                    if pattern in result:
                        result = result.replace(pattern, replacement, 1)
                        break
                if result != old_result:
                    break

            # 2. Regex ile gevşek eşleme (model placeholder'ı biraz değiştirmiş olabilir)
            if result == old_result:
                for idx_var in idx_variants:
                    loose_pattern = rf'@@\s*TERM\s*{idx_var}\s*@@'
                    new_result = re.sub(loose_pattern, replacement, result, count=1, flags=re.IGNORECASE)
                    if new_result != result:
                        result = new_result
                        break

            # 3. Eski XML tag formatı (geriye uyumluluk)
            if result == old_result:
                xml_pattern = rf'<\s*term\s+id\s*=\s*"{idx:04d}"[^>]*>([^<]*)<\s*/\s*term\s*>'
                result = re.sub(xml_pattern, replacement, result, flags=re.IGNORECASE)

            if result != old_result:
                restored_count += 1
                if logger:
                    logger.glossary(f"  Geri yuklendi: idx={idx} -> '{replacement}'", "DEBUG")
            else:
                not_found.append(idx)

        # POST-PASS: Kalan XML tag'leri ve placeholder'ları temizle
        result = self._post_pass_cleanup(result, protected)

        if logger:
            logger.glossary(f"restore_terms bitti: {restored_count}/{len([p for p in protected if p.get('mode') == 'hard'])} terim geri yuklendi", "DEBUG")
            if not_found:
                logger.glossary(f"  Bulunamayan placeholder'lar: {not_found}", "WARNING")

        return result, not_found

    def _post_pass_cleanup(self, text: str, protected: List[dict]) -> str:
        """
        Post-pass: Kalan placeholder'ları temizle
        """
        result = text

        # CORE-B07: O(N) lineer arama yerine dict lookup
        protected_by_idx = {item['index']: item for item in protected}

        # 1. Kalan @@TERM...@@ placeholder'ları temizle
        remaining = re.findall(r'@@\s*TERM\s*\d+\s*@@', result, re.IGNORECASE)
        remaining += re.findall(r'@@\s*TERIM\s*\d+\s*@@', result, re.IGNORECASE)

        for placeholder in remaining:
            match = re.search(r'(\d+)', placeholder)
            if match:
                idx = int(match.group(1))
                item = protected_by_idx.get(idx)
                if item:
                    if item.get("keep_original", False):
                        replacement = item["original"]
                    else:
                        replacement = self._match_case(item["original"], item["term_tr"])
                    result = result.replace(placeholder, replacement)

        # 2. Eski XML tag formatı (geriye uyumluluk)
        xml_remaining = re.findall(r'<\s*term\s+id\s*=\s*"(\d+)"[^>]*>([^<]*)<\s*/\s*term\s*>', result, re.IGNORECASE)
        for idx_str, content in xml_remaining:
            idx = int(idx_str)
            item = protected_by_idx.get(idx)
            if item:
                if item.get("keep_original", False):
                    replacement = item["original"]
                else:
                    replacement = self._match_case(item["original"], item["term_tr"])
                pattern = rf'<\s*term\s+id\s*=\s*"{idx_str}"[^>]*>[^<]*<\s*/\s*term\s*>'
                result = re.sub(pattern, replacement, result, flags=re.IGNORECASE)

        return result

    def reset_first_occurrence(self):
        """Ilk gecis takibini sifirla (yeni dokuman icin)"""
        self._first_occurrence_used = set()

    def _match_case(self, original: str, replacement: str) -> str:
        """Orijinal metnin büyük/küçük harf durumunu koru"""
        # None veya boş string kontrolü
        if not original or not replacement:
            return replacement or ""
        if original.isupper():
            return replacement.upper()
        elif original[0].isupper():
            return replacement.capitalize()
        else:
            return replacement.lower()

    def find_terms_in_text(self, text: str) -> List[dict]:
        """Metinde geçen terimleri bul"""
        found = []

        for pattern, term_en, term_data in self.term_patterns:
            matches = pattern.findall(text)
            if matches:
                found.append({
                    "term_en": term_en,
                    "term_tr": term_data["tr"],
                    "count": len(matches),
                    "context": term_data.get("context", "")
                })

        return found

    def suggest_term(self, text: str, threshold: int = 80) -> Optional[dict]:
        """Benzer terim öner (fuzzy matching)"""
        if not self.terms:
            return None

        term_list = list(self.terms.keys())
        result = process.extractOne(text.lower(), term_list, scorer=fuzz.ratio)

        if result and result[1] >= threshold:
            matched_term = result[0]
            return {
                "suggested": matched_term,
                "score": result[1],
                "data": self.terms[matched_term]
            }

        return None

    def get_term(self, term_en: str) -> Optional[dict]:
        """Terim bilgisini getir"""
        return self.terms.get(term_en.lower())

    def export_to_file(self, output_path: str, format: str = "json"):
        """Terimleri dosyaya aktar"""
        # Mevcut dosyayı yedekle (overwrite koruması)
        output_file = Path(output_path)
        if output_file.exists():
            backup_path = str(output_file) + ".bak"
            try:
                import shutil
                shutil.copy(output_path, backup_path)
            except Exception as e:
                print(f"  Backup oluşturulamadı: {e}")

        if format == "json":
            with open(output_path, 'w', encoding='utf-8') as f:
                json.dump({"terms": self.terms}, f, ensure_ascii=False, indent=2)

        elif format == "csv":
            import csv
            with open(output_path, 'w', encoding='utf-8', newline='') as f:
                writer = csv.writer(f)
                writer.writerow(["English", "Turkish", "Context", "Note"])
                for term_data in self.terms.values():
                    writer.writerow([
                        term_data["en"],
                        term_data["tr"],
                        term_data.get("context", ""),
                        term_data.get("note", "")
                    ])

    def get_stats(self) -> dict:
        """İstatistikleri getir"""
        sources = {}
        for term_data in self.terms.values():
            source = term_data.get("source", "unknown")
            sources[source] = sources.get(source, 0) + 1

        return {
            "total_terms": len(self.terms),
            "by_source": sources
        }


# Test
if __name__ == "__main__":
    gm = GlossaryManager()

    test_text = """
    The stress in the material depends on the applied force and the cross-sectional area.
    According to Newton's second law, F = ma, where F is the force, m is the mass,
    and a is the acceleration. The eigenvalue of the matrix determines stability.
    """

    print("Orijinal metin:")
    print(test_text)
    print("\n" + "="*50)

    # Terimleri bul
    found = gm.find_terms_in_text(test_text)
    print("\nBulunan terimler:")
    for item in found:
        print(f"  {item['term_en']} → {item['term_tr']} ({item['count']}x)")

    # Terimleri koru
    protected_text, protected_list = gm.protect_terms(test_text)
    print("\nKorunan metin:")
    print(protected_text)

    # Terimleri geri koy
    restored = gm.restore_terms(protected_text, protected_list, use_turkish=True)
    print("\nTürkçe terimlerle:")
    print(restored)
