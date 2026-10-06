"""K1 — çeviri belleği model başına ayrı (KARARLAR 8, 25 Eylül 2026).

- Varsayılan motor (``registry.DEFAULT_ENGINE``, HY-MT 7B) bugünkü proje
  anahtarını AYNEN kullanır: mevcut kayıtlar HY-MT'ye ait kalır, taşınmaz.
- Diğer motorlar ``"<proje>@<motor kimliği>"`` havuzunu kullanır (tek
  yardımcı: ``core.memory.memory_project_key``).
- İki motor aynı metni çevirir -> birbirinin kaydını görmez; hymt-7b eski
  kaydı görür.

Bellek her testte geçici bir SQLite dosyasıdır (``memory_db_path``);
data/translation_memory*.db'ye dokunulmaz. Model yüklenmez: sahte Ollama /
OpenAI uyumlu sunucu + ``create_engine`` yaması. Girdiler sentetiktir.
"""

from __future__ import annotations

import pytest

import core.mt_engines as cme
from core.memory import MEMORY_ENGINE_SEP, TranslationMemory, memory_project_key
from core.mt_engines import registry
from core.mt_engines.base import EngineSpec
from core.mt_engines.chat import ChatTemplateEngine
from core.translator import BookTranslator, TranslationConfig

PROJECT = "sentetik-kitap"
SOURCE = "The weather is nice today."


# ---------------------------------------------------------------------------
# Yardımcı fonksiyon (tek kaynak)
# ---------------------------------------------------------------------------
class TestAnahtar:
    def test_varsayilan_motor_bugunku_anahtar(self):
        assert memory_project_key(PROJECT, registry.DEFAULT_ENGINE) == PROJECT

    def test_diger_motor_ayri_havuz(self):
        assert memory_project_key(PROJECT, "ollama:qwen3:8b") == f"{PROJECT}@ollama:qwen3:8b"
        assert MEMORY_ENGINE_SEP == "@"

    @pytest.mark.parametrize("engine", [None, "", "   "])
    def test_motor_bilinmiyorsa_bugunku_anahtar(self, engine):
        assert memory_project_key(PROJECT, engine) == PROJECT

    def test_proje_adi_yoksa_default(self):
        assert memory_project_key(None, registry.DEFAULT_ENGINE) == "default"
        assert memory_project_key("", "mlx:qwen3-8b-mlx") == "default@mlx:qwen3-8b-mlx"

    def test_bosluk_kirpilir(self):
        assert memory_project_key(PROJECT, f" {registry.DEFAULT_ENGINE} ") == PROJECT

    def test_varsayilan_registryden_okunur(self, monkeypatch):
        # Tek kaynak: sabit kopyalanmaz, çağrı anında registry.DEFAULT_ENGINE okunur.
        monkeypatch.setattr(registry, "DEFAULT_ENGINE", "baska-varsayilan")
        assert memory_project_key(PROJECT, "baska-varsayilan") == PROJECT
        assert memory_project_key(PROJECT, "hymt-7b") == f"{PROJECT}@hymt-7b"


# ---------------------------------------------------------------------------
# BookTranslator._init_memory anahtarı kullanır
# ---------------------------------------------------------------------------
def _skeleton_with_memory(engine_id, db_path):
    """Model yüklemeden yalnız belleği kur (_init_memory gerçek kod)."""
    bt = BookTranslator.__new__(BookTranslator)
    bt.config = TranslationConfig(mt_engine=engine_id, project_name=PROJECT,
                                  memory_db_path=str(db_path))
    bt.memory = None
    bt.stats = {"memory_entries_loaded": 0}
    bt._init_memory()
    assert bt.memory is not None
    return bt


class TestInitMemory:
    def test_hymt_eski_kaydi_gorur(self, tmp_path):
        db = tmp_path / "tm.db"
        with TranslationMemory(db_path=str(db), project_name=PROJECT) as old:
            assert old.add(SOURCE, "Bugün hava güzel.")          # bugünkü (HY-MT) kayıt
        bt = _skeleton_with_memory(registry.DEFAULT_ENGINE, db)
        assert bt.memory.project_name == PROJECT
        assert bt.memory.get(SOURCE)["target_text"] == "Bugün hava güzel."
        bt.memory.close()

    def test_diger_motor_eski_kaydi_gormez(self, tmp_path):
        db = tmp_path / "tm.db"
        with TranslationMemory(db_path=str(db), project_name=PROJECT) as old:
            old.add(SOURCE, "Bugün hava güzel.")
        bt = _skeleton_with_memory("ollama:qwen3:8b", db)
        assert bt.memory.project_name == f"{PROJECT}@ollama:qwen3:8b"
        assert bt.memory.get(SOURCE) is None
        assert bt.memory.get_or_similar(SOURCE, 80)[0] is None
        bt.memory.close()


# ---------------------------------------------------------------------------
# Uçtan uca: iki motor aynı metni çevirir (KARARLAR 8 testi)
# ---------------------------------------------------------------------------
@pytest.fixture
def engines(fake_llm_server, monkeypatch):
    """İki sahte motor: A (Ollama) ve B (OpenAI uyumlu), farklı çeviriler üretir."""
    srv_a = fake_llm_server("ollama", reply=lambda t: "A modeli: bugün hava güzel.")
    srv_b = fake_llm_server("openai", reply=lambda t: "B modeli: bugün hava çok güzel.")
    specs = {
        "ollama:qwen3:8b": EngineSpec(name="ollama:qwen3:8b", format="chat", backend="ollama",
                                      model="qwen3:8b", base_url=srv_a.url, source="adhoc"),
        "openai:yerel|qwen": EngineSpec(name="openai:yerel|qwen", format="chat",
                                        backend="openai", model="qwen",
                                        base_url=srv_b.base_url, source="adhoc"),
    }
    monkeypatch.setattr(cme, "create_engine",
                        lambda name, *a, **k: ChatTemplateEngine(specs[name]))
    return {"ollama:qwen3:8b": srv_a, "openai:yerel|qwen": srv_b}


def _translator(engine_id, db):
    return BookTranslator(TranslationConfig(
        mt_engine=engine_id, project_name=PROJECT, memory_db_path=str(db),
        use_memory=True, glossary_mode="off", run_qa=False, check_consistency=False))


def _gen_requests(srv):
    return srv.requests_to("/api/chat") + [
        b for b in srv.requests_to("/v1/chat/completions") if b.get("max_tokens", 0) > 1]


class TestIkiMotor:
    def test_birbirinin_kaydini_gormez(self, engines, tmp_path):
        db = tmp_path / "tm.db"
        bt_a = _translator("ollama:qwen3:8b", db)
        bt_b = _translator("openai:yerel|qwen", db)
        assert bt_a.translate_text(SOURCE) == "A modeli: bugün hava güzel."
        # B, A'nın kaydını bellekten DÖNDÜRMEZ; kendi modeline gider
        assert bt_b.translate_text(SOURCE) == "B modeli: bugün hava çok güzel."
        assert len(_gen_requests(engines["openai:yerel|qwen"])) == 1
        assert bt_b.stats["segments_from_memory"] == 0

        # Aynı motorla yeni koşu kendi kaydını bellekten alır (modele gitmez)
        n_a = len(_gen_requests(engines["ollama:qwen3:8b"]))
        bt_a2 = _translator("ollama:qwen3:8b", db)
        assert bt_a2.translate_text(SOURCE) == "A modeli: bugün hava güzel."
        assert bt_a2.stats["segments_from_memory"] == 1
        assert len(_gen_requests(engines["ollama:qwen3:8b"])) == n_a

        # Havuzlar veritabanında ayrı
        with TranslationMemory(db_path=str(db), project_name=PROJECT) as tm:
            by_project = tm.get_stats()["by_project"]
        assert by_project == {f"{PROJECT}@ollama:qwen3:8b": 1,
                              f"{PROJECT}@openai:yerel|qwen": 1}
        for bt in (bt_a, bt_b, bt_a2):
            bt.memory.close()

    def test_toplu_yol_da_ayri(self, engines, tmp_path):
        db = tmp_path / "tm.db"
        bt_a = _translator("ollama:qwen3:8b", db)
        assert bt_a.translate_batch_protected([SOURCE]) == ["A modeli: bugün hava güzel."]
        bt_b = _translator("openai:yerel|qwen", db)
        assert bt_b.translate_batch_protected([SOURCE]) == ["B modeli: bugün hava çok güzel."]
        for bt in (bt_a, bt_b):
            bt.memory.close()

    def test_hymt_eski_kaydi_gorur_digerlerini_gormez(self, engines, tmp_path):
        db = tmp_path / "tm.db"
        with TranslationMemory(db_path=str(db), project_name=PROJECT) as old:
            old.add(SOURCE, "HY-MT: bugün hava güzel.")          # mevcut HY-MT kaydı
        bt_a = _translator("ollama:qwen3:8b", db)
        # Yeni motor eski HY-MT kaydını görmez, modele gider
        assert bt_a.translate_text(SOURCE) == "A modeli: bugün hava güzel."
        bt_a.memory.close()
        # hymt-7b (model yüklemeden, yalnız bellek) eski kaydını AYNEN görür;
        # A'nın kaydı ona karışmaz
        bt_h = _skeleton_with_memory(registry.DEFAULT_ENGINE, db)
        assert bt_h.memory.get(SOURCE)["target_text"] == "HY-MT: bugün hava güzel."
        assert bt_h.memory.get_stats()["by_project"][PROJECT] == 1
        bt_h.memory.close()
