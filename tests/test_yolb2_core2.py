"""
Yol B DALGA 2 -- core2 grubu fix testleri (core/translator.py).

Kapsam (bulgu -> test):
- CORE-07: _dedup_segments 1:1 sozlesme kirilimi -> translate_segments artik
  GIRDI ile AYNI sayida segment dondurur (tekrar eden metinler bir kez cevrilir,
  index map ile tum pozisyonlara dagitilir). zip site'leri (_translate_pdf /
  _translate_epub QA + export) ve EPUB checkpoint resume offset'i hizada kalir.
- BATCH-03: MT uretim cagrilari class-level threading.Lock ile serilestirilir
  (zombi thread + eszamanli generate riski). Dar kilit -> OOM rekursiyonunda
  deadlock yok.
- BATCH-04: batch yolunda 512 max_tokens truncation -> _scale_max_tokens girdi
  uzunluguna gore olcekler (cap: config.defaults.MT_OUTPUT_TOKENS_CAP; kural
  2026-09-28'den beri config/defaults.py'deki sabitlerle, KARARLAR 15).
- CORE-B4: lang_names sozlugu module-level tek sabit (_HYMT_LANG_NAMES).

Strateji: BookTranslator.__new__ ile __init__ ATLANIR; MT katmani mock'lanir.
"""

import sys
import threading
import time
import types
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config.defaults import (
    MT_CONTEXT_CHARS_PER_TOKEN,
    MT_OUTPUT_TOKENS_CAP,
    MT_OUTPUT_TOKENS_FACTOR,
)
from core.translator import (
    BookTranslator,
    TranslationConfig,
    _HYMT_LANG_NAMES,
)

# 3000 karakterlik girdinin beklenen sınırı (KARARLAR 15: karakter / 3 × 2,2)
_SCALED_3000 = int(3000 / MT_CONTEXT_CHARS_PER_TOKEN * MT_OUTPUT_TOKENS_FACTOR)


def make_bt(**cfg):
    """__init__ atlayarak gercek metodlu BookTranslator; MT yuklenmez."""
    bt = BookTranslator.__new__(BookTranslator)
    defaults = dict(use_memory=False, glossary_mode="off", batch_size=4,
                    run_qa=False, check_consistency=False)
    defaults.update(cfg)
    bt.config = TranslationConfig(**defaults)
    bt.memory = None
    bt.glossary = None
    bt.qa_checker = None
    bt.consistency_analyzer = None
    bt.naturalizer = None
    bt.ner_protector = None
    bt._use_mlx = True
    bt._dict_loader = None
    bt.mt_model = MagicMock()
    bt.mt_tokenizer = MagicMock()
    bt.stats = {k: 0 for k in (
        "segments_translated", "segments_from_memory", "terms_enforced",
        "ai_reviews", "retried_segments", "consistency_fixes",
        "glossary_terms_loaded", "memory_entries_loaded",
    )}
    return bt


# ============================================================================
# CORE-07: translate_segments 1:1 sozlesme
# ============================================================================


class TestCore07OneToOne:
    def _fake_batch(self, bt):
        # _translate_batch_segments'i deterministik 1:1 mock'la
        bt._translate_batch_segments = lambda texts: [f"TR::{t}" for t in texts]

    def test_output_length_equals_input(self):
        bt = make_bt(batch_size=3)
        self._fake_batch(bt)
        segs = [{"text": f"Segment number {i} content here."} for i in range(10)]
        out = bt.translate_segments(list(segs))
        assert len(out) == 10

    def test_consecutive_duplicates_not_dropped(self):
        """Eski dedup ardisik tekrari SILIYORDU -> girdi>cikti. Artik korunur."""
        bt = make_bt(batch_size=8)
        self._fake_batch(bt)
        segs = [
            {"text": "This is a long enough sentence."},
            {"text": "This is a long enough sentence."},   # ardisik dup
            {"text": "Different content over here now."},
        ]
        out = bt.translate_segments(list(segs))
        assert len(out) == 3
        # ardisik ikizi ayni cevriyi paylasir
        assert out[0]["text"] == out[1]["text"] == "TR::This is a long enough sentence."

    def test_alignment_preserved(self):
        """Her cikti segmentinin 'original'i girdi metniyle birebir hizali."""
        bt = make_bt(batch_size=3)
        self._fake_batch(bt)
        texts = ["Alpha alpha alpha one.", "Beta beta beta two.",
                 "Alpha alpha alpha one.", "Gamma gamma gamma three.",
                 "Beta beta beta two."]
        segs = [{"text": t} for t in texts]
        out = bt.translate_segments(list(segs))
        for i, t in enumerate(texts):
            assert out[i]["original"] == t
            assert out[i]["text"] == f"TR::{t}"

    def test_duplicates_translated_once(self):
        """Verimlilik: ayni metin MT'ye bir kez gider (benzersiz sayisi kadar)."""
        seen = []
        bt = make_bt(batch_size=100)

        def spy_batch(texts):
            seen.extend(texts)
            return [f"TR::{t}" for t in texts]

        bt._translate_batch_segments = spy_batch
        texts = ["dup sentence long enough", "dup sentence long enough",
                 "dup sentence long enough", "unique sentence long enough"]
        bt.translate_segments([{"text": t} for t in texts])
        # 4 segment ama sadece 2 benzersiz metin MT'ye gitti
        assert len(seen) == 2
        assert sorted(seen) == ["dup sentence long enough", "unique sentence long enough"]

    def test_whitespace_variants_share_translation(self):
        """Whitespace farki benzersizligi bozmaz (key normalize edilir)."""
        seen = []
        bt = make_bt(batch_size=100)

        def spy_batch(texts):
            seen.extend(texts)
            return [f"TR::{t}" for t in texts]

        bt._translate_batch_segments = spy_batch
        segs = [
            {"text": "Same  sentence   with   spaces."},
            {"text": "Same sentence with spaces."},
        ]
        out = bt.translate_segments(list(segs))
        assert len(out) == 2
        assert len(seen) == 1  # tek benzersiz metin

    def test_batch_exception_fallback_marks_mt_failed(self):
        """Batch patlarsa tek-tek fallback; basarisiz benzersiz -> _mt_failed."""
        bt = make_bt(batch_size=8)

        def boom(texts):
            raise RuntimeError("batch broke")

        bt._translate_batch_segments = boom
        # translate_text: bir metinde patlar, digerinde calisir
        def fake_single(text, **kw):
            if "boom" in text:
                raise ValueError("single broke")
            return f"S::{text}"

        bt.translate_text = fake_single
        segs = [
            {"text": "good sentence one here."},
            {"text": "boom sentence fails here."},
            {"text": "good sentence one here."},   # dup of first -> ayni cevri
        ]
        out = bt.translate_segments(list(segs))
        assert len(out) == 3
        # ilk ve ucuncu ayni (dup), ikisi de basarili
        assert out[0]["text"] == out[2]["text"] == "S::good sentence one here."
        # ikinci basarisiz -> orijinal korunur + _mt_failed
        assert out[1]["text"] == "boom sentence fails here."
        assert out[1].get("_mt_failed") is True

    def test_epub_checkpoint_offset_alignment(self):
        """
        CORE-07'nin checkpoint hizalamasina etkisi: _translate_epub batch
        dongusu her batch icin translate_segments(batch) cagirir ve
        translated_segments'e EXTEND eder; sonra start_index=len(cached).
        translate_segments GIRDI kadar dondururse toplam uzunluk segment
        sayisiyla birebir buyur -> offset kaymaz. Burada gercek translate_segments
        ile bu invariant'i dogruluyoruz (dedup silme olsaydi bozulurdu).
        """
        bt = make_bt(batch_size=4)
        self._fake_batch(bt)
        # Icinde ardisik + ardisik-olmayan tekrarlar olan segment kumesi
        all_segments = [
            {"text": "Chapter heading repeated line."},
            {"text": "Chapter heading repeated line."},   # ardisik dup
            {"text": "Body paragraph number one here."},
            {"text": "Body paragraph number two here."},
            {"text": "Chapter heading repeated line."},    # ardisik-olmayan dup
            {"text": "Body paragraph number three here."},
        ]
        # _translate_epub'in batch dongusunu birebir taklit et
        epub_batch_size = 4
        translated = []
        for i in range(0, len(all_segments), epub_batch_size):
            batch = all_segments[i:i + epub_batch_size]
            bt_translated = bt.translate_segments(batch)
            translated.extend(bt_translated)
        # Checkpoint invariant: toplam cevrilen == toplam segment
        assert len(translated) == len(all_segments)
        # Hizalama: her pozisyon dogru orijinalle eslesir
        for i, seg in enumerate(all_segments):
            assert translated[i]["original"] == seg["text"]


# ============================================================================
# BATCH-04: max_tokens olcekleme
# ============================================================================


class TestBatch04Scaling:
    def test_short_input_stays_base(self):
        bt = make_bt()
        assert bt._scale_max_tokens(["short"], base=512) == 512

    def test_medium_input_scales(self):
        bt = make_bt()
        # 3000 char -> est 1000 token -> *2.2 = 2200 (config/defaults.py)
        assert bt._scale_max_tokens(["a" * 3000], base=512) == _SCALED_3000

    def test_long_input_capped(self):
        bt = make_bt()
        assert bt._scale_max_tokens(["a" * 100000], base=512) == MT_OUTPUT_TOKENS_CAP

    def test_uses_longest_in_batch(self):
        bt = make_bt()
        texts = ["short", "a" * 3000, "also short"]
        assert bt._scale_max_tokens(texts, base=512) == _SCALED_3000

    def test_never_below_base(self):
        bt = make_bt()
        # base 900 > scaled(short)=512 -> base korunur
        assert bt._scale_max_tokens(["x"], base=900) == 900

    def test_empty_and_none_safe(self):
        bt = make_bt()
        assert bt._scale_max_tokens([], base=512) == 512
        assert bt._scale_max_tokens([None, ""], base=512) == 512

    def test_wired_into_mlx_batch_path(self):
        """_translate_batch_mlx uzun girdide generate'e scaled max_tokens gecirir."""
        captured = {}

        def fake_generate(model, tok, prompt="", max_tokens=0):
            captured["max_tokens"] = max_tokens
            return "CEVIRI"

        fake_mlx = types.ModuleType("mlx_lm")
        fake_mlx.generate = fake_generate
        # batch_generate YOK -> sirali generate yolu (tek metin)
        sys.modules["mlx_lm"] = fake_mlx
        try:
            bt = make_bt()
            bt._use_mlx = True
            long_text = "a" * 3000
            bt._translate_batch_mlx([long_text], max_length=512)
            assert captured["max_tokens"] == _SCALED_3000  # scaled
        finally:
            sys.modules.pop("mlx_lm", None)


# ============================================================================
# BATCH-03: uretim serilestirme + deadlock yok
# ============================================================================


class TestBatch03Lock:
    def test_lock_is_class_level_shared(self):
        a = make_bt()
        b = make_bt()
        assert a._mt_generate_lock is b._mt_generate_lock
        assert a._mt_generate_lock is BookTranslator._mt_generate_lock

    def test_generate_calls_serialized(self):
        active = {"n": 0, "max": 0}
        counter_lock = threading.Lock()

        def fake_generate(model, tok, prompt="", max_tokens=0):
            with counter_lock:
                active["n"] += 1
                active["max"] = max(active["max"], active["n"])
            time.sleep(0.03)
            with counter_lock:
                active["n"] -= 1
            return "CEVIRI"

        fake_mlx = types.ModuleType("mlx_lm")
        fake_mlx.generate = fake_generate
        sys.modules["mlx_lm"] = fake_mlx
        try:
            bt = make_bt()
            bt._use_mlx = True

            def run_one():
                bt._translate_with_hymt_mlx("sentence to translate here",
                                            "English", "Turkish", 512, False)

            threads = [threading.Thread(target=run_one) for _ in range(5)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            # Ayni anda en fazla 1 generate calismis olmali
            assert active["max"] == 1
        finally:
            sys.modules.pop("mlx_lm", None)

    def test_no_deadlock_on_oom_recursion(self):
        """
        Transformers batch OOM'de batch ikiye bolunerek REKURSIF cagrilir.
        Kilit DAR (sadece generate'i sarar) oldugundan rekursiyon deadlock
        yapmamali. generate ilk cagrida OOM firlatir, sonra basarili olur.
        """
        bt = make_bt(batch_size=8)
        bt._use_mlx = False

        state = {"calls": 0}

        class Model:
            # 'device' attr YOK -> .to() dali atlanir
            def generate(self, **kw):
                state["calls"] += 1
                if state["calls"] == 1:
                    raise RuntimeError("CUDA out of memory")

                # Basit indexlenebilir/iterlenebilir cikti (batch boyutu kadar satir)
                bs = kw["input_ids"].shape[0]

                class Row:
                    def __getitem__(self, s):
                        return [1, 2, 3, 4]

                class Out:
                    def __init__(self, n):
                        self._n = n

                    def __iter__(self):
                        return iter([Row() for _ in range(self._n)])

                    def __getitem__(self, i):
                        return Row()

                return Out(bs)

        bt.mt_model = Model()

        # tokenizer: input_ids.shape = [batch, seq]
        def tok_call(msgs, **kw):
            n = len(msgs) if isinstance(msgs, list) else 1
            return {"input_ids": types.SimpleNamespace(shape=[n, 3])}

        bt.mt_tokenizer = MagicMock(side_effect=tok_call)
        bt.mt_tokenizer.apply_chat_template = lambda *a, **k: "CHAT"
        bt.mt_tokenizer.eos_token_id = 0
        bt.mt_tokenizer.decode = lambda g, **kw: "decoded"

        # 2 metin -> OOM -> ikiye bolunur (1+1), her biri basarili.
        # Hang olursa test timeout ile degil, mantik ile biter (deadlock yoksa doner).
        out = bt._translate_batch_hymt(["first text here", "second text here"], max_length=512)
        assert len(out) == 2
        assert state["calls"] >= 2  # ilk OOM + bolunmus cagrilar


# ============================================================================
# CORE-B4: module-level lang_names sabiti
# ============================================================================


class TestCoreB4LangNames:
    def test_constant_has_all_langs(self):
        assert _HYMT_LANG_NAMES["en"] == "English"
        assert _HYMT_LANG_NAMES["tr"] == "Turkish"
        assert len(_HYMT_LANG_NAMES) == 12

    def test_hymt_reads_constant(self, monkeypatch):
        """_translate_with_hymt config dillerini _HYMT_LANG_NAMES'ten cozer."""
        bt = make_bt(source_lang="de", target_lang="fr")
        captured = {}

        def fake_mlx(text, src, tgt, max_len, is_heading=False):
            captured["src"] = src
            captured["tgt"] = tgt
            return "X"

        bt._translate_with_hymt_mlx = fake_mlx
        bt._use_mlx = True
        bt._translate_with_hymt("hallo welt", 512)
        assert captured["src"] == "German"
        assert captured["tgt"] == "French"
