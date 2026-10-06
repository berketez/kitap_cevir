"""
NER Protector - Ozel Isim Koruma Modulu
Kisi isimleri, kitap adlari, kurum isimleri vb. korur
"""

import re
from typing import Dict, List, Tuple, Set
from pathlib import Path
import json


class NERProtector:
    """
    Ozel isimleri tespit edip koruma altina alir.

    Kategoriler:
    - PERSON: Kişi adları (ör. Ada Lovelace, Alan Turing)
    - ORG: Kurumlar (Kinsey Institute, Harvard)
    - WORK: Eser adları (ör. Principia Mathematica)
    - TERM: Bilimsel terimler (anterior cingulate cortex)

    Codex önerileri uygulandı:
    - ASCII placeholder formatı (@@NER_0001@@) - model bozamaz
    - Toleranslı restore (regex ile varyasyonları yakala)
    - Post-pass ile kalan UNKNOWN_X temizle
    """

    # YENİ Placeholder formatı - ASCII, model bozamaz (Codex önerisi)
    PLACEHOLDER_FORMAT = "@@{category}{index:04d}@@"

    # Restore için toleranslı regex pattern'ları
    RESTORE_PATTERNS = [
        # Standart format: @@PERSON0001@@
        r'@@\s*{category}\s*{index}\s*@@',
        # Boşluklu: @@ PERSON 0001 @@
        r'@@\s*{category}\s+{index}\s*@@',
        # Tire ile: @@PERSON-0001@@
        r'@@\s*{category}[-_]?{index}\s*@@',
        # Eski format: [[PERSON_0001]]
        r'\[\[\s*{category}[_-]?{index}\s*\]\]',
        # Türkçe çeviri: @@KİŞİ0001@@ veya @@BILINMEYEN_0001@@
        r'@@\s*(?:KISI|BILINMEYEN|UNKNOWN)\s*{index}\s*@@',
        # Sadece numara kalmış: @@0001@@
        r'@@\s*{index}\s*@@',
    ]

    # 2026-09-29 (bug avı F3-04): "ardışık Büyük harfli sözcükler = özel ad"
    # sezgisinin (UNKNOWN) bulduğu diziler artık MT'den SAKLANMAZ. Sezgi
    # İngilizceye göre yazılmıştı; saklanan dizi çevrilmeden, özgün hâliyle
    # geri konuyordu: İngilizcede cümle başı "The Earth", "The Sun", "Third
    # Law", "Low Earth Orbit", "Soviet Union" (Türkçesi "Sovyetler Birliği");
    # Almancada neredeyse her cümle başı ("Die Wand", "Ein Zahlenbeispiel",
    # "Bei Stahl" — Almanca her adı büyük harfle yazar); fr/it/es'te "La
    # Terre", "Il Sole". Derlem (salt okuma): bir Almanca test kitabında 57
    # paragrafın 41'i, bir İngilizce test kitabında 753'ün 60'ı etkileniyordu;
    # gerçek HY-MT çıktısında "The Earth'ın".
    # Kişi adlarını model zaten korur; Türkçede kurum/yer adlarının yerleşik
    # çevirisi vardır. Bilinen terimler (do_not_translate.json) korunmaya
    # devam eder. ``hide_unknown=True`` eski davranışı açıkça ister.
    HIDE_UNKNOWN_DEFAULT = False

    # İngilizce cümle başında büyük yazılan işlev sözcükleri: aday dizinin
    # başından atılır ("The Earth" -> "Earth", tek sözcük -> aday değil).
    _LEADING_FUNCTION_WORDS = frozenset({
        "the", "a", "an", "this", "that", "these", "those", "its", "his", "her",
        "their", "our", "my", "your", "for", "in", "on", "at", "by", "with",
        "from", "to", "of", "as", "if", "when", "while", "since", "after",
        "before", "during", "because", "although", "and", "but", "or", "so",
        "then", "thus", "hence", "however", "all", "each", "every", "some",
        "many", "most", "both", "no", "not", "there", "here", "we", "it",
    })

    def __init__(self, glossary_dir: str = None, source_lang: str = "en",
                 hide_unknown: bool = HIDE_UNKNOWN_DEFAULT):
        """
        Args:
            glossary_dir: Glossary dizini (do_not_translate.json icin)
            source_lang: Kaynak dil (ISO 639-1). Büyük harf dizisi sezgisi
                yalnız İngilizcede aday üretir (2026-09-29, F3-04).
            hide_unknown: True ise sezginin adayları da MT'den saklanır
                (eski davranış; varsayılan kapalı).
        """
        self.glossary_dir = glossary_dir or self._get_default_dir()
        self.source_lang = source_lang or "en"
        self.hide_unknown = hide_unknown

        # Korunacak terimler
        self.protected_entities: Dict[str, Dict] = {}

        # Kategorilere gore listeler
        self.persons: Set[str] = set()
        self.organizations: Set[str] = set()
        self.works: Set[str] = set()
        self.terms: Set[str] = set()

        # Yukle
        self._load_entities()

    def _get_default_dir(self) -> str:
        """Varsayilan glossary dizini"""
        return str(Path(__file__).parent.parent / "glossary")

    def _load_entities(self):
        """do_not_translate.json'dan yukle"""
        json_path = Path(self.glossary_dir) / "do_not_translate.json"

        if not json_path.exists():
            return

        try:
            with open(json_path, 'r', encoding='utf-8') as f:
                data = json.load(f)

            for term, info in data.get("terms", {}).items():
                note = info.get("note", "").lower()
                self.protected_entities[term.lower()] = {
                    "original": term,
                    "translation": info.get("tr", term),
                    "keep_original": info.get("keep_original", False),
                    "note": info.get("note", "")
                }

                # Kategorize et
                if "yazar" in note or "kisi" in note:
                    self.persons.add(term.lower())
                elif "kurum" in note or "universite" in note:
                    self.organizations.add(term.lower())
                elif "kitap" in note:
                    self.works.add(term.lower())
                else:
                    self.terms.add(term.lower())

        except Exception as e:
            print(f"[NER] Yukleme hatasi: {e}")

    def detect_entities(self, text: str) -> List[Tuple[str, str, int, int]]:
        """
        Metinde ozel isimleri tespit et

        Returns:
            [(entity, category, start, end), ...]
        """
        entities = []

        # 1. Bilinen terimleri ara
        # 2026-07-11: kelime SINIRI eklendi — eskiden 'MIT' limit/permit/
        # submitted icinde, 'Ada' adaptasyon icinde eslesip kelime-ortasi
        # placeholder uretiyordu (sub@@ORG0003@@ted). \b yerine lookaround:
        # 'C++' gibi kenari kelime-karakteri olmayan terimlerde \b calismaz.
        # 2026-09-29 (F3-04): kısaltma terimi (tamamı büyük harf, "MIT")
        # büyük/küçük harfe DUYARLI aranır — eskiden Almanca 'mit' edatı,
        # Fransızca 'mit' fiili ORG sayılıp çevrilmeden kalıyordu.
        for term, info in self.protected_entities.items():
            original = info.get("original", term) if isinstance(info, dict) else term
            if self._is_acronym(original):
                pattern = re.compile(r"(?<!\w)" + re.escape(original) + r"(?!\w)")
            else:
                pattern = re.compile(
                    r"(?<!\w)" + re.escape(term) + r"(?!\w)", re.IGNORECASE
                )
            for match in pattern.finditer(text):
                category = self._get_category(term)
                entities.append((match.group(), category, match.start(), match.end()))

        # 2. Buyuk harfle baslayan ardisik kelimeleri tespit et (potansiyel ozel isim)
        # Ornek: "John Smith", "New York Times". 2026-09-29 (F3-04): yalnız
        # İngilizce kaynakta (başka dillerde büyük harf kuralı farklı: Almanca
        # her adı büyük yazar); cümle başı işlev sözcüğü diziden atılır. Aday
        # varsayılan olarak MT'den SAKLANMAZ (bkz. HIDE_UNKNOWN_DEFAULT).
        if (getattr(self, "source_lang", "en") or "en") == "en":
            entities.extend(self._unknown_candidates(text))

        # Pozisyona gore sirala
        entities.sort(key=lambda x: x[2])

        return entities

    @staticmethod
    def _is_acronym(term: str) -> bool:
        """Tamamı büyük harf, en az iki harfli kısaltma mı ("MIT", "NASA")?"""
        letters = [ch for ch in term if ch.isalpha()]
        return len(letters) >= 2 and term.isupper()

    _PROPER_NOUN_RE = re.compile(r'\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+)+)\b')

    def _unknown_candidates(self, text: str) -> List[Tuple[str, str, int, int]]:
        """İngilizce büyük harf dizisi adayları (baştaki işlev sözcüğü atılmış)."""
        found = []
        for match in self._PROPER_NOUN_RE.finditer(text):
            words = list(re.finditer(r"[A-Z][a-z]+", match.group()))
            while words and words[0].group().lower() in self._LEADING_FUNCTION_WORDS:
                words.pop(0)
            if len(words) < 2:
                continue
            start = match.start() + words[0].start()
            end = match.start() + words[-1].end()
            entity = text[start:end]
            if entity.lower() not in self.protected_entities:
                found.append((entity, "UNKNOWN", start, end))
        return found

    def _get_category(self, term: str) -> str:
        """Terimin kategorisini bul"""
        term_lower = term.lower()
        if term_lower in self.persons:
            return "PERSON"
        elif term_lower in self.organizations:
            return "ORG"
        elif term_lower in self.works:
            return "WORK"
        elif term_lower in self.terms:
            return "TERM"
        return "UNKNOWN"

    def protect(self, text: str) -> Tuple[str, Dict[str, str]]:
        """
        Ozel isimleri placeholder ile degistir

        Args:
            text: Orijinal metin

        Returns:
            (korunmus metin, {placeholder: orijinal} mapping)
        """
        entities = self.detect_entities(text)
        # 2026-09-29 (F3-04): sezginin adayları (UNKNOWN) varsayılan olarak
        # MT'den saklanmaz; bilinen terimler saklanır.
        if not getattr(self, "hide_unknown", self.HIDE_UNKNOWN_DEFAULT):
            entities = [e for e in entities if e[1] != "UNKNOWN"]

        if not entities:
            return text, {}

        # 2026-07-11: CAKISAN entity'ler offset aritmetigini bozuyordu
        # ('New York' + 'New York Times' ayni bolgede -> ikinci degisim
        # kaymis konuma yazip metni cogaltiyor/yok ediyordu). Baslangica
        # gore sirala (esitlikte UZUN olan once), onceki kabul edilen
        # aralikla cakisani ele.
        entities.sort(key=lambda x: (x[2], -(x[3] - x[2])))
        selected = []
        last_end = -1
        for ent in entities:
            if ent[2] >= last_end:
                selected.append(ent)
                last_end = ent[3]
        entities = selected

        mapping = {}
        result = text
        offset = 0  # Karakter kaymasini takip et

        for i, (entity, category, start, end) in enumerate(entities):
            # YENİ FORMAT: @@PERSON0001@@ (Codex önerisi - ASCII, 4 haneli index)
            placeholder = f"@@{category}{i:04d}@@"

            # Pozisyonu offset ile ayarla
            adjusted_start = start + offset
            adjusted_end = end + offset

            # Degistir
            result = result[:adjusted_start] + placeholder + result[adjusted_end:]

            # Mapping kaydet - hem placeholder hem de metadata
            mapping[placeholder] = {
                "original": entity,
                "category": category,
                "index": i
            }

            # Offset guncelle
            offset += len(placeholder) - (end - start)

        return result, mapping

    def restore(self, text: str, mapping: Dict, use_translation: bool = False) -> str:
        """
        Placeholder'lari geri koy (Codex önerisi: toleranslı restore)

        Args:
            text: Placeholder'li metin
            mapping: protect'ten donen mapping
            use_translation: True ise ceviriyi kullan (varsa)

        Returns:
            Restore edilmis metin
        """
        result = text

        for placeholder, item in mapping.items():
            # Yeni format: item dict olabilir
            if isinstance(item, dict):
                original = item["original"]
                category = item["category"]
                index = item["index"]
            else:
                # Eski format: item string
                original = item
                # Placeholder'dan category ve index çıkar
                match = re.search(r'@@(\w+?)(\d+)@@', placeholder)
                if match:
                    category = match.group(1)
                    index = int(match.group(2))
                else:
                    category = "UNKNOWN"
                    index = 0

            if use_translation:
                # Ceviri var mi bak
                info = self.protected_entities.get(original.lower(), {})
                if info.get("keep_original", False):
                    replacement = original
                else:
                    replacement = info.get("translation", original)
            else:
                replacement = original

            # 1. Önce tam eşleşme dene
            if placeholder in result:
                result = result.replace(placeholder, replacement)
                continue

            # 2. TOLERANSLI RESTORE (Codex önerisi)
            # Model placeholder'ı bozmuş olabilir - varyasyonları dene
            restored = False

            # Index'i farklı formatlarda ara (0001, 1, 01, vb.)
            index_variants = [f"{index:04d}", str(index), f"{index:02d}"]

            for idx_var in index_variants:
                # Farklı pattern varyasyonları
                patterns = [
                    f"@@{category}{idx_var}@@",           # @@PERSON0001@@
                    f"@@ {category} {idx_var} @@",        # @@ PERSON 0001 @@
                    f"@@{category}_{idx_var}@@",          # @@PERSON_0001@@
                    f"@@{category}-{idx_var}@@",          # @@PERSON-0001@@
                    f"[[{category}_{idx_var}]]",          # [[PERSON_0001]]
                    f"[[{category}{idx_var}]]",           # [[PERSON0001]]
                    f"@@UNKNOWN{idx_var}@@",              # Model UNKNOWN'a çevirmiş
                    f"@@BILINMEYEN{idx_var}@@",           # Türkçeye çevirmiş
                    f"@@BILINMEYEN_{idx_var}@@",          # Türkçe + alt çizgi
                ]

                # İşaretsiz kalıntılar (@@ düşmüş): düz substring ARAMAZ —
                # 'UNKNOWN_1' deseni 'UNKNOWN_10' içinde eşleşip entity 1'i
                # yanlış konuma basıyor, entity 10 kalıntısını bozuyordu
                # (F44). Sözcük sınırı + ardından rakam gelmeme koşulu.
                bare_patterns = [
                    rf"(?<!\w)UNKNOWN[_]{idx_var}(?!\d)",      # işaretsiz
                    rf"(?<!\w)BILINMEYEN[_]{idx_var}(?!\d)",   # Türkçe, işaretsiz
                ]

                for pattern in patterns:
                    if pattern in result:
                        result = result.replace(pattern, replacement, 1)
                        restored = True
                        break

                if not restored:
                    for bare in bare_patterns:
                        # replacement ham metin olarak yazılır (\1 gibi grup
                        # kaçışları yorumlanmasın diye lambda kullanılır)
                        new_result, n_sub = re.subn(
                            bare, lambda _m: replacement, result, count=1
                        )
                        if n_sub:
                            result = new_result
                            restored = True
                            break

                if restored:
                    break

        # 3. POST-PASS: Kalan UNKNOWN_\d+ ve BILINMEYEN_\d+ temizle (Codex önerisi)
        result = self._post_pass_cleanup(result, mapping)

        return result

    # Bozulmuş placeholder kalıntısı desenleri (Unicode-farkındalıklı).
    # MT modeli @@UNKNOWN0000@@ gibi placeholder'ların SÖZCÜK kısmını
    # çevirebiliyor (UNKNOWN → BİLİNMİYOR) veya @@ işaretlerini kısmen
    # düşürebiliyor. Kanıt (sutton smoke, 2026-07-06):
    #   "OSCAR BIBLARZ"            → "MİYOR0000@@"
    #   "Gravity-Free Drag-Free"   → "Yerçekimi-MİYOR0000-MİYOR0001"
    # Eski desenler ([A-Z_]+) Türkçe 'İ' harfini yakalayamadığı için bu
    # kalıntılar final çıktıya sızıyordu. Yanlış-pozitif koruması:
    # yakalanan index mapping'de yoksa metne DOKUNULMAZ.
    _RESIDUE_PATTERNS = [
        # @@<herhangi-sözcük>0001@@  (tam sarılı, sözcük bozulmuş)
        re.compile(r'@@[^\s@]{0,24}?(\d{1,6})@@'),
        # MİYOR0001@@  (baştaki @@ düşmüş)
        re.compile(r'[^\W\d_]{2,24}(\d{4})@@'),
        # @@MİYOR0001  (sondaki @@ düşmüş)
        re.compile(r'@@[^\W\d_]{2,24}(\d{4})(?!\d)'),
        # İşaretsiz kalıntı: bilinen kategori-sözcük gövdeleri + index
        # (MT'nin UNKNOWN/PERSON çevirilerinde gözlenen varyantlar)
        re.compile(
            r'\b(?:UNKNOWN|B[İI]L[İI]NMEYEN|B[İI]L[İI]NM[İI]YOR|M[İI]YOR|'
            r'PERSON|K[İI][ŞS][İI]|ORG|KURUM|WORK|ESER|TERM|TER[İI]M)'
            r'[-_]?(\d{1,4})(?!\d)\b'
        ),
    ]

    def _post_pass_cleanup(self, text: str, mapping: Dict) -> str:
        """
        Post-pass: Kalan/bozulmuş placeholder varyasyonlarını orijinaline
        geri yükle (Codex önerisi + 2026-07-06 MİYOR kalıntısı sertleştirmesi)
        """
        if not mapping:
            return text

        # index → orijinal metin haritası
        index_to_original: Dict[int, str] = {}
        for placeholder, item in mapping.items():
            if isinstance(item, dict):
                index_to_original[item["index"]] = item["original"]
            else:
                m = re.search(r'(\d+)', placeholder)
                if m:
                    index_to_original[int(m.group(1))] = item

        if not index_to_original:
            return text

        result = text
        for pattern in self._RESIDUE_PATTERNS:
            def _resolve(m: "re.Match") -> str:
                try:
                    idx = int(m.group(1))
                except (ValueError, IndexError):
                    return m.group(0)
                original = index_to_original.get(idx)
                # Index mapping'de yoksa yanlış pozitiftir — dokunma
                return original if original is not None else m.group(0)

            result = pattern.sub(_resolve, result)

        return result

    def add_entity(self, entity: str, category: str = "UNKNOWN",
                   translation: str = None, keep_original: bool = True):
        """
        Yeni ozel isim ekle

        Args:
            entity: Ozel isim
            category: PERSON, ORG, WORK, TERM
            translation: Turkce karsiligi (None ise ayni kalir)
            keep_original: Orijinali koru mu?
        """
        self.protected_entities[entity.lower()] = {
            "original": entity,
            "translation": translation or entity,
            "keep_original": keep_original,
            "note": category
        }

        # Kategorize et
        if category == "PERSON":
            self.persons.add(entity.lower())
        elif category == "ORG":
            self.organizations.add(entity.lower())
        elif category == "WORK":
            self.works.add(entity.lower())
        else:
            self.terms.add(entity.lower())

    def get_stats(self) -> Dict:
        """Istatistikler"""
        return {
            "total": len(self.protected_entities),
            "persons": len(self.persons),
            "organizations": len(self.organizations),
            "works": len(self.works),
            "terms": len(self.terms)
        }


# Test
if __name__ == "__main__":
    ner = NERProtector()

    test_text = """
    According to a report from the Kinsey Institute, the anterior cingulate
    cortex plays a role in decision making. Researchers at Harvard and MIT
    also studied this phenomenon.
    """

    print("NER Protector Test\n" + "="*50)
    print(f"Orijinal:\n{test_text}\n")

    # Tespit
    entities = ner.detect_entities(test_text)
    print(f"Tespit edilen varliklar ({len(entities)}):")
    for ent, cat, start, end in entities:
        print(f"  [{cat}] {ent}")

    # Koruma
    protected, mapping = ner.protect(test_text)
    print(f"\nKorunmus metin:\n{protected}")

    # Geri yukleme
    restored = ner.restore(protected, mapping, use_translation=True)
    print(f"\nRestore edilmis:\n{restored}")

    # Istatistikler
    print(f"\nIstatistikler: {ner.get_stats()}")
