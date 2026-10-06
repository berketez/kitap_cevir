"""Y2 kulvarı (çevirmen çekirdeği) bug avı düzeltmeleri — 29 Eylül 2026.

Bulgular (bug avı, 2026-09-28):
  F3-04  NERProtector'ın büyük harf dizisi sezgisi kaynak dil sözcüklerini
         çıktıda bırakıyordu ("The Earth", "Die Wand", Almanca "mit" = MIT).
  F3-05  Belleğin %90 bulanık eşleşmesi zıt anlamlı segmente başka segmentin
         çevirisini veriyordu (increases/decreases, efficient/not efficient).
  F3-06  Yarım çeviri belleğe yazılıyor, yeniden çeviri aynı yarımı geri alıyordu.
  F3-07  Üretim sınırına dayanan (kesik) çeviri belleğe yazılıyordu.
  F4-02  MLX sohbet modelinde önsöz / tırnak / "Not:" temizlenmiyordu.
  F4-09  Her MLX modeli günlükte "HY-MT ... WMT25 şampiyonu" diye anılıyordu.
  F3-14  SentenceSplitter ASCII dışı büyük harfte bölmüyor, satır sonlarını siliyordu.
  F3-15 / F4-12  Bellek havuzu anahtarında '@'/'#' çakışması.

Girdiler sentetiktir (kitap metni kopyalanmadı). Model YÜKLENMEZ, ağa
gidilmez: arka uç ve ``mlx_lm`` süreç içi sahtedir; bellek geçici SQLite.
"""

from __future__ import annotations

import sys
import types
from collections import defaultdict

import pytest

from core.memory import TranslationMemory
from core.mt_engines.contracts import GenerationResult
from core.ner_protector import NERProtector
from core.sentence_splitter import SentenceSplitter
from core.translator import BookTranslator, TranslationConfig


# ---------------------------------------------------------------------------
# Yardımcılar
# ---------------------------------------------------------------------------
_TR_WORDS = ("yörünge", "güneş", "ışık", "kütle", "değer", "şekil", "düzlem", "eğri")


def fake_turkish(text: str) -> str:
    """Kararlı sahte 'çeviri': her sözcük Türkçe harfli bir sözcüğe (test altyapısı)."""
    import re
    import zlib

    def word(m):
        return _TR_WORDS[zlib.crc32(m.group(0).lower().encode("utf-8")) % len(_TR_WORDS)]

    return re.sub(r"(?<!@)\b[^\W\d_]+\b(?!@)", word, text)


class FakeBackend:
    """Sunucu arka ucu sahtesi: istek metinlerini kaydeder, ``reply`` ile yanıtlar.

    ``reply(text)`` bir str (finish_reason "stop") ya da GenerationResult döner.
    """

    def __init__(self, reply=fake_turkish):
        self.reply = reply
        self.texts = []

    def _one(self, request):
        self.texts.append(request.text)
        out = self.reply(request.text)
        if isinstance(out, GenerationResult):
            return out
        return GenerationResult(text=out, finish_reason="stop")

    def generate(self, engine, request):
        return self._one(request)

    def generate_batch(self, engine, requests):
        return [self._one(r) for r in requests]


def make_bt(tmp_path, *, backend=None, source_lang="en", engine="ollama:sahte",
            memory=True, ner=None, project="kitap"):
    """Gerçek BookTranslator iskeleti: model yüklemeden, sahte arka uçla."""
    bt = BookTranslator.__new__(BookTranslator)
    bt.config = TranslationConfig(
        mt_engine=engine, source_lang=source_lang, glossary_mode="off",
        run_qa=False, check_consistency=False, use_memory=memory,
        memory_db_path=str(tmp_path / "bellek.db"), project_name=project,
        book_type="science")
    bt.mt_model = None
    bt.mt_tokenizer = None
    bt._backend = backend
    bt._use_mlx = False
    bt.ai_client = None
    bt.glossary = None
    bt.qa_checker = None
    bt.consistency_analyzer = None
    bt.naturalizer = None
    bt._dict_loader = None
    bt.splitter = SentenceSplitter()
    bt.ner_protector = ner
    bt.stats = defaultdict(int)
    bt.memory = None
    if memory:
        bt._init_memory()
    return bt


def seed_memory(tmp_path, pairs, project="kitap"):
    """Belleğe kaynak dil kapısı OLMADAN kayıt yaz (eski dönemden kalan kayıt gibi)."""
    tm = TranslationMemory(str(tmp_path / "bellek.db"), project + "@ollama:sahte")
    for src, tgt in pairs:
        assert tm.add(src, tgt)
    tm.close()


# ===========================================================================
# F3-04 — NERProtector
# ===========================================================================
class TestF304Ner:
    def test_ingilizce_cumle_basi_tanimlik_saklanmaz(self):
        text = "The Earth moves around the star once every year."
        protected, mapping = NERProtector().protect(text)
        assert mapping == {}
        assert protected == text

    def test_ingilizce_baslik_dizisi_saklanmaz(self):
        # "The Moon Orbit" / "Low Earth Orbit": Türkçesi olan ad öbekleri MT'ye gider
        text = "The Moon Orbit is tilted; a Low Earth Orbit is lower."
        _, mapping = NERProtector().protect(text)
        assert all(v["category"] != "UNKNOWN" for v in mapping.values())

    def test_almanca_buyuk_harf_dizisi_aday_degil(self):
        p = NERProtector()
        p.source_lang = "de"
        text = "Die Wand ist mit Stahl verstärkt. Ein Zahlenbeispiel zeigt den Wärmestrom."
        protected, mapping = p.protect(text)
        assert mapping == {}
        assert protected == text

    def test_kisaltma_buyuk_kucuk_harfe_duyarli(self):
        # glossary/do_not_translate.json: "MIT" (kurum). Almanca "mit" edatı,
        # Fransızca "mit" fiili terim DEĞİL.
        p = NERProtector()
        assert not [e for e in p.detect_entities("Die Wand ist mit Stahl verstärkt.")
                    if e[0].lower() == "mit"]
        assert not [e for e in p.detect_entities("Il mit le livre sur la table.")
                    if e[0].lower() == "mit"]
        found = [e for e in p.detect_entities("The data came from MIT last year.")
                 if e[0] == "MIT"]
        assert len(found) == 1 and found[0][1] == "ORG"

    def test_bilinen_ad_korunmaya_devam_eder(self):
        # glossary/do_not_translate.json'daki bir kurum adı (kişi adı sözlükte
        # yok: tek kitaba özgü adlar 5 Ekim 2026'da çıkarıldı).
        protected, mapping = NERProtector().protect("Washington Post wrote about it.")
        assert [v["original"] for v in mapping.values()] == ["Washington Post"]
        assert "Washington Post" not in protected

    def test_eski_davranis_acikca_istenebilir(self):
        p = NERProtector(hide_unknown=True)
        _, mapping = p.protect("The Moon Orbit is tilted by five degrees.")
        # Cümle başı tanımlık diziden atılır: aday "Moon Orbit"
        assert [v["original"] for v in mapping.values()] == ["Moon Orbit"]

    def test_uctan_uca_ingilizce_ad_modele_gorunur(self, tmp_path):
        backend = FakeBackend()
        bt = make_bt(tmp_path, backend=backend, memory=False, ner=NERProtector())
        out = bt.translate_text("The Earth moves around the star once every year.")
        assert backend.texts == ["The Earth moves around the star once every year."]
        assert "The Earth" not in out and "@@" not in out

    def test_uctan_uca_almanca_toplu_yol(self, tmp_path):
        backend = FakeBackend()
        ner = NERProtector()
        ner.source_lang = "de"
        bt = make_bt(tmp_path, backend=backend, memory=False, ner=ner, source_lang="de")
        src = ["Die Wand ist mit Stahl verstärkt und mit Beton gefüllt.",
               "Bei Stahl ist die Leitfähigkeit viel größer als bei Holz."]
        out = bt.translate_batch_protected(src)
        assert backend.texts == src                     # hiçbir sözcük saklanmadı
        for line in out:
            for word in ("Die", "Wand", "mit", "Bei", "Stahl"):
                assert word not in line.split()


# ===========================================================================
# F3-05 — bulanık eşleşme otomatik yeniden kullanımda kapalı
# ===========================================================================
_INC = "The energy increases with the radius of the orbit in this simple model."
_DEC = "The energy decreases with the radius of the orbit in this simple model."


class TestF305Bulanik:
    @pytest.mark.parametrize("stored,asked", [
        (_INC, _DEC),
        ("This method is efficient for large orbits around heavy stars.",
         "This method is not efficient for large orbits around heavy stars."),
        ("The maximum speed is reached near the focus of the ellipse.",
         "The minimum speed is reached near the focus of the ellipse."),
        ("Paragraph number one describes another property of the orbit.",
         "Paragraph number two describes another property of the orbit."),
        ("Is the orbit of the planet stable over long times?",
         "Is the orbit of the planet stable over long times."),
    ])
    def test_farkli_sozcuk_ya_da_noktalama_donmez(self, tmp_path, stored, asked):
        tm = TranslationMemory(str(tmp_path / "b.db"), "kitap")
        assert tm.add(stored, "Sabit bir çeviri cümlesi burada duruyor.")
        assert tm.get_or_similar(asked, 90) == (None, "none")
        tm.close()

    def test_yalniz_bosluk_ve_harf_buyuklugu_farki_doner(self, tmp_path):
        tm = TranslationMemory(str(tmp_path / "b.db"), "kitap")
        assert tm.add("The force , acts on the body (see below).", "Kuvvet cisme etki eder.")
        got = tm.get_or_similar("the force,  acts on the\nbody (see below).", 90)
        assert got == ("Kuvvet cisme etki eder.", "similar")
        tm.close()

    def test_almanca_eszett_sadelestirilmez(self, tmp_path):
        # casefold "Maße" (ölçüler) ile "Masse" (kütle)'yi eşitlerdi
        tm = TranslationMemory(str(tmp_path / "b.db"), "kitap#de")
        assert tm.add("Die Maße des Körpers sind bekannt.", "Cismin ölçüleri bilinir.")
        assert tm.get_or_similar("Die Masse des Körpers sind bekannt.", 90) == (None, "none")
        tm.close()

    def test_toplu_yolda_zit_anlamli_segment_modele_gider(self, tmp_path):
        seed_memory(tmp_path, [(_INC, "Enerji yörünge yarıçapıyla ARTAR.")])
        backend = FakeBackend(lambda t: "Enerji yörünge yarıçapıyla AZALIR.")
        bt = make_bt(tmp_path, backend=backend)
        out = bt.translate_batch_protected([_INC, _DEC])
        assert backend.texts == [_DEC]
        assert out == ["Enerji yörünge yarıçapıyla ARTAR.", "Enerji yörünge yarıçapıyla AZALIR."]


# ===========================================================================
# F3-06 — yarım çeviri belleğe yazılmaz / bellekten dönmez
# ===========================================================================
_SRC = "The engine is fired twice at carefully chosen points of the path to reach the higher orbit safely."
_HALF = "The engine is fired twice at carefully chosen points of the path to reach the higher yörünge safely."
_FULL = "Motor, daha yüksek yörüngeye güvenle ulaşmak için yolun özenle seçilmiş iki noktasında ateşlenir."


class TestF306YarimCeviri:
    def test_eski_yarim_kayit_bellekten_donmez(self, tmp_path):
        seed_memory(tmp_path, [(_SRC, _HALF)])
        backend = FakeBackend(lambda t: _FULL)
        bt = make_bt(tmp_path, backend=backend)
        assert bt.translate_text(_SRC) == _FULL          # yeniden çeviri modele gitti
        assert backend.texts == [_SRC]
        assert bt.memory.get(_SRC)["target_text"] == _FULL   # kayıt güncellendi

    def test_yarim_ceviri_yazilmaz(self, tmp_path):
        calls = []

        def reply(text):
            calls.append(text)
            return _HALF if len(calls) == 1 else _FULL

        bt = make_bt(tmp_path, backend=FakeBackend(reply))
        assert bt.translate_text(_SRC) == _HALF          # kabul (QC/yeniden çeviri görür)
        assert bt.memory.get(_SRC) is None
        assert bt.translate_text(_SRC) == _FULL          # ikinci çağrı modele gider
        assert len(calls) == 2

    def test_almanca_kaynakta_yarim_ceviri_yazilmaz(self, tmp_path):
        src = "Das Triebwerk wird zweimal an genau gewählten Punkten der Bahn gezündet."
        half = "Das Triebwerk wird zweimal an genau gewählten Punkten der yörünge gezündet."
        bt = make_bt(tmp_path, backend=FakeBackend(lambda t: half), source_lang="de")
        bt.translate_text(src)
        assert bt.memory.get(src) is None

    def test_tam_turkce_ceviri_yazilir(self, tmp_path):
        bt = make_bt(tmp_path, backend=FakeBackend(lambda t: _FULL))
        bt.translate_text(_SRC)
        assert bt.memory.get(_SRC)["target_text"] == _FULL

    def test_kaynak_dili_verilmeyen_bellek_eski_davranis(self, tmp_path):
        tm = TranslationMemory(str(tmp_path / "b.db"), "kitap")
        assert tm.add(_SRC, _HALF)                       # kapı yok (geriye uyum)
        tm.close()


# ===========================================================================
# F3-07 — sınıra dayanan (kesik) çeviri belleğe yazılmaz, rapora taşınır
# ===========================================================================
_LONG = ("The total energy of the planet depends only on the size of the orbit, which is a "
         "surprising result for most students of mechanics.")
_OK = "A planet moves along an elliptical path around the star in a year."


def _cut(text):
    words = fake_turkish(text).split()
    return GenerationResult(text=" ".join(words[: len(words) // 3]),
                            finish_reason="length", output_tokens=8)


def _reply_cut(text):
    return _cut(text) if "total energy" in text else fake_turkish(text)


class TestF307Kesik:
    def test_tekil_yol_bellege_yazmaz_ve_rapor_verir(self, tmp_path):
        bt = make_bt(tmp_path, backend=FakeBackend(_reply_cut))
        out = bt.translate_text(_LONG)
        assert out == _cut(_LONG).text                   # KARARLAR 15: kabul
        assert bt.memory.get(_LONG) is None
        assert bt.stats["segments_truncated"] == 1
        report = bt.truncation_report()
        assert report == [{"source": _LONG, "output": out, "used": 8, "limit": report[0]["limit"]}]
        assert report[0]["limit"] >= 512

    def test_toplu_korumali_yol(self, tmp_path):
        bt = make_bt(tmp_path, backend=FakeBackend(_reply_cut))
        out = bt.translate_batch_protected([_OK, _LONG])
        assert bt.memory.get(_OK)["target_text"] == out[0]
        assert bt.memory.get(_LONG) is None
        assert [r["source"] for r in bt.truncation_report()] == [_LONG]

    def test_toplu_segment_yolu(self, tmp_path):
        bt = make_bt(tmp_path, backend=FakeBackend(_reply_cut))
        out = bt._translate_batch_segments([_LONG, _OK])
        assert bt.memory.get(_OK)["target_text"] == out[1]
        assert bt.memory.get(_LONG) is None

    def test_uzun_metnin_cumlesi_kesilirse_segment_yazilmaz(self, tmp_path):
        long_text = " ".join([_OK] * 6 + [_LONG])       # > 512: cümlelere bölünür
        assert len(long_text) > 512
        bt = make_bt(tmp_path, backend=FakeBackend(_reply_cut))
        bt.translate_text(long_text)
        assert bt.memory.get(long_text) is None
        assert [r["source"] for r in bt.truncation_report()] == [long_text]

    def test_toplu_yolda_alt_dize_baska_segmenti_baglamaz(self, tmp_path):
        short = "The planet keeps a constant total energy along the orbit."
        longer = short + " " + _OK
        bt = make_bt(tmp_path, backend=FakeBackend(
            lambda t: _cut(t) if t == short else fake_turkish(t)))
        bt.translate_batch_protected([longer, short])
        assert bt.memory.get(longer) is not None
        assert bt.memory.get(short) is None
        assert [r["source"] for r in bt.truncation_report()] == [short]

    def test_kesilmeyen_ceviri_raporda_yok(self, tmp_path):
        bt = make_bt(tmp_path, backend=FakeBackend())
        bt.translate_text(_OK)
        assert bt.truncation_report() == []
        assert bt.memory.get(_OK) is not None


# ===========================================================================
# F4-02 — MLX sohbet modeli çıktısı sunucu arka ucuyla aynı temizlenir
# ===========================================================================
_RAW = 'İşte çeviri: "Yörünge çok basık bir elipstir."\n\nNot: Terimler korunmuştur.'
_SRC_ORBIT = "The orbit is a very flat ellipse."


class _PlainChatTok:
    """Düşünme belirtisi OLMAYAN sohbet şablonlu sahte tokenizer."""

    chat_template = "{% for m in messages %}{{ m.content }}{% endfor %}"

    def apply_chat_template(self, messages, tokenize=False, add_generation_prompt=True, **kw):
        return "\n".join(m["content"] for m in messages)

    def encode(self, text, add_special_tokens=False):
        return text.split()


def _mlx_bt(monkeypatch, engine, raw):
    fake = types.ModuleType("mlx_lm")
    fake.generate = lambda model, tokenizer, prompt="", max_tokens=0, **kw: raw
    monkeypatch.setitem(sys.modules, "mlx_lm", fake)   # batch_generate yok: sıralı yol
    bt = BookTranslator.__new__(BookTranslator)
    bt.config = TranslationConfig(mt_engine=engine, use_memory=False, glossary_mode="off",
                                  run_qa=False, check_consistency=False)
    bt.mt_model, bt.mt_tokenizer = object(), _PlainChatTok()
    bt._use_mlx, bt._backend = True, None
    bt.stats = defaultdict(int)
    return bt


class TestF402MlxTemizlik:
    def test_sohbet_modeli_tekil_ve_toplu(self, monkeypatch):
        bt = _mlx_bt(monkeypatch, "mlx:Qwen2.5-7B-Instruct-MLX", _RAW)
        assert bt._translate_with_hymt(_SRC_ORBIT) == "Yörünge çok basık bir elipstir."
        assert bt._translate_batch_hymt([_SRC_ORBIT, _SRC_ORBIT]) == [
            "Yörünge çok basık bir elipstir."] * 2

    def test_hymt_ciktisi_degismez(self, monkeypatch):
        # HY-MT biçimi kaynağı kullanmaz: yalnız özel token temizliği (aynen)
        raw = 'İşte çeviri: "Yörünge."<|eos|>'
        bt = _mlx_bt(monkeypatch, "hymt-7b", raw)
        assert bt._translate_with_hymt(_SRC_ORBIT) == 'İşte çeviri: "Yörünge."'


# ===========================================================================
# F4-09 — günlükte motorun kendi adı
# ===========================================================================
class _LogRecorder:
    def __init__(self):
        self.lines = []

    def translator(self, message, level="INFO"):
        self.lines.append(message)

    def __getattr__(self, name):   # diğer kanallar (cache_hit vb.) sessiz
        return lambda *a, **k: None


def _init_log(monkeypatch, tmp_path, engine):
    import platform

    from core import translator as tr_mod

    rec = _LogRecorder()
    monkeypatch.setattr(tr_mod, "logger", rec)
    fake = types.ModuleType("mlx_lm")
    fake.load = lambda path, **kw: (object(), _PlainChatTok())
    monkeypatch.setitem(sys.modules, "mlx_lm", fake)
    monkeypatch.setattr(platform, "system", lambda: "Darwin")
    bt = BookTranslator.__new__(BookTranslator)
    bt.config = TranslationConfig(mt_engine=engine)
    model_dir = tmp_path / "model"
    model_dir.mkdir()
    eng = bt.engine
    monkeypatch.setattr(type(eng), "resolve_local_model_dir", lambda self, models_dir: model_dir)
    bt._init_hymt_model("model-kimligi")
    return "\n".join(rec.lines)


class TestF409Gunluk:
    def test_baska_mlx_modeli_hymt_diye_anilmaz(self, monkeypatch, tmp_path):
        log = _init_log(monkeypatch, tmp_path, "mlx:Qwen2.5-7B-Instruct-MLX")
        assert "Qwen2.5-7B-Instruct-MLX" in log
        assert "HY-MT" not in log and "WMT25" not in log

    def test_hymt_tanitimi_yalniz_yerlesik_kayitta(self, monkeypatch, tmp_path):
        log = _init_log(monkeypatch, tmp_path, "hymt-7b")
        assert "HY-MT1.5 7B" in log and "WMT25" in log


# ===========================================================================
# F3-14 — SentenceSplitter: Unicode büyük harf + satır sonları
# ===========================================================================
class TestF314CumleBolucu:
    @pytest.mark.parametrize("text,n", [
        ("Die Wärme fließt durch die Wand. Über die Oberfläche geht sie weg. "
         "Äußere Schichten sind kälter. Öfen erzeugen die Wärme.", 4),
        ("Теплота передаётся через стену. Когда стена толстая, поток мал. "
         "Это важно для инженеров.", 3),
        ("La chaleur traverse le mur. À l'extérieur, l'air est froid. "
         "Élever la température coûte cher.", 3),
    ])
    def test_ascii_disi_buyuk_harfte_bolunur(self, text, n):
        assert len(SentenceSplitter().split(text)) == n

    def test_satir_sonlari_korunur(self):
        text = "Line one of text here\n% a comment line\nand more words after it here."
        assert SentenceSplitter().split(text) == [text]

    def test_ayraclar_ozgun(self):
        text = "Sentence one here.\n% A comment line.\nSentence two here.\n\nNew paragraph starts."
        assert SentenceSplitter().split_with_separators(text) == [
            ("Sentence one here.\n% A comment line.", "\n"),
            ("Sentence two here.", "\n\n"),
            ("New paragraph starts.", ""),
        ]

    def test_ingilizce_tek_satir_ayni(self):
        text = ("The committee reviewed the decision carefully. Stop now. "
                "It was rejected by the board yesterday.")
        assert SentenceSplitter().split(text) == [
            "The committee reviewed the decision carefully. Stop now.",
            "It was rejected by the board yesterday."]

    def test_uzun_metinde_yorumdan_sonraki_cumle_yoruma_dusmez(self, tmp_path):
        first = "The first sentence of this long paragraph explains the orbit in detail. " * 7
        text = (first.strip() + "\n% A comment line that must stay a comment.\n"
                "The last sentence of the paragraph must stay outside of the comment.")
        assert len(text) > 512
        bt = make_bt(tmp_path, backend=FakeBackend(lambda t: t.upper()), memory=False)
        out = bt._translate_with_mt(text)
        comment_line = [l for l in out.split("\n") if l.startswith("%")]
        assert comment_line == ["% A COMMENT LINE THAT MUST STAY A COMMENT."]
        assert out.split("\n")[-1] == ("THE LAST SENTENCE OF THE PARAGRAPH MUST STAY "
                                       "OUTSIDE OF THE COMMENT.")


# ===========================================================================
# F3-15 / F4-12 — bellek havuzu anahtarı ayrıştırılabilir
# ===========================================================================
class TestF315Anahtar:
    def _pool(self, tmp_path, name, lang, engine):
        bt = BookTranslator.__new__(BookTranslator)
        bt.stats = {}
        bt.config = types.SimpleNamespace(
            memory_db_path=str(tmp_path / "b.db"), project_name=name,
            mt_engine=engine, source_lang=lang)
        bt._init_memory()
        key = bt.memory.project_name
        bt.memory.close()
        return key

    @pytest.mark.parametrize("a,b", [
        (("Notlar", "de", "ollama:qwen3:8b"), ("Notlar#de", "en", "ollama:qwen3:8b")),
        (("Notlar", "en", "ollama:qwen3:8b"), ("Notlar@ollama:qwen3:8b", "en", "hymt-7b")),
        (("Kitap", "de", "hymt-7b"), ("Kitap#de", "en", "hymt-7b")),
        (("Kitap%40x", "en", "hymt-7b"), ("Kitap@x", "en", "hymt-7b")),
    ])
    def test_farkli_uclu_farkli_havuz(self, tmp_path, a, b):
        assert self._pool(tmp_path, *a) != self._pool(tmp_path, *b)

    def test_ingilizce_ve_hymt_anahtarlari_aynen(self, tmp_path):
        assert self._pool(tmp_path, "kitap", "en", "hymt-7b") == "kitap"
        assert self._pool(tmp_path, None, "en", "hymt-7b") == "default"
        assert self._pool(tmp_path, "kitap", "en", "ollama:qwen3:8b") == "kitap@ollama:qwen3:8b"
        assert self._pool(tmp_path, "kitap", "de", "hymt-7b") == "kitap#de"
        assert self._pool(tmp_path, "kitap", "de", "ollama:qwen3:8b") == "kitap#de@ollama:qwen3:8b"

    def test_tek_yardimci_ve_ters_cozum(self):
        from core.memory import (memory_pool_key, memory_pool_project, memory_project_key,
                                 parse_memory_pool_key)

        for name, lang, engine in (("Notlar#de", "en", "ollama:qwen3:8b"),
                                   ("C# @ 100%", "fr", "mlx:Qwen"),
                                   ("kitap", "en", "hymt-7b")):
            key = memory_pool_key(name, lang, engine)
            assert key == memory_project_key(memory_pool_project(name, lang), engine)
            assert parse_memory_pool_key(key) == (
                name, lang, None if engine == "hymt-7b" else engine)
