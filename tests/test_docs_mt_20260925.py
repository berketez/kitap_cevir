"""Yerel model dokümanları (K4) — 25 Eylül 2026.

"Yerel model ile çevir" yolu dört model kaynağına açıldı (MLX klasörü,
Ollama, LM Studio / OpenAI uyumlu sunucu, GGUF dosyası). Kullanıcı
dokümanları bunu sözleşmeyle (core/mt_engines/contracts.py,
config/defaults.py) AYNI anlatıyor mu:

- kurulum komutu, kimlik önekleri, süreler ve ileti metinleri sözleşmeden
  okunur ve dokümanda aynen geçer (kopya metin sapmasın);
- varsayılan sunucu adresleri dokümanda TEKRAR YAZILMAZ, config/defaults.py'ye
  atıf yapılır (KARARLAR 13, 28 Eylül 2026: tek kaynak);
- config/*.json.example dosyaları kaldırıldı (KARARLAR 13); nadir elle ayar
  durumu docs/YENI_MODEL_EKLEME.md'deki satır içi kısa JSON örnekleriyle
  anlatılır: örnekler geçerlidir, yalnız bu bilgisayarı gösterir, model
  klasörü eklemez ve kod onları uyarısız yükler;
- yerel model yolunun kaynak dili her dokümanda TEK yerde söylenir (K5 o yeri
  günceller; cümle dağılmaz);
- kullanıcı dokümanında iç adlar (Yol A/B, ATLAS, VECTOR) ve ASCII'ye kaçmış
  Türkçe yoktur.

Testler model, ağ ya da XeLaTeX GEREKTİRMEZ.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import re
import string
import urllib.parse
from pathlib import Path

import pytest

from config import defaults
from core.lang_detect import TEX_SOURCE_LANGS, language_list_tr
from core.mt_engines import contracts, registry
from core.mt_engines.base import EngineSpec
from core.mt_engines.http_util import check_local_url, is_loopback_host
from tests.fakes import write_fake_gguf, write_fake_mlx_dir
from ui import branding

PROJECT_ROOT = Path(__file__).resolve().parent.parent

MODEL_DOC = "docs/YENI_MODEL_EKLEME.md"
SETUP_DOC = "docs/KURULUM_VE_KULLANIM.md"
README = "README.md"
TEX_DOC = "docs/Kullanma_Kilavuzu.tex"
USER_DOCS = (MODEL_DOC, SETUP_DOC, README, TEX_DOC)
# Ayar dosyalarının adları (config/ altında; isteğe bağlı, git dışı).
CONFIG_FILENAMES = (contracts.MT_SELECTION_FILENAME, contracts.MT_SOURCES_FILENAME,
                    contracts.MT_ENGINES_FILENAME)
EXAMPLE_SUFFIX = ".example"
# Varsayılan sunucu adreslerinin tek kaynağı (KARARLAR 13).
DEFAULT_URL_NAMES = ("OLLAMA_DEFAULT_URL", "LMSTUDIO_DEFAULT_URL")
DEFAULT_URLS = tuple(getattr(defaults, name) for name in DEFAULT_URL_NAMES)

# Katalogdaki sayım kalıpları ("{n} model") ileti değil, durum satırıdır.
TROUBLE_MESSAGES = sorted(name for name in contracts.MESSAGE_CATALOG
                          if not name.startswith("MSG_SOURCE_COUNT_"))

# Dokümandaki örnek iletiler bu değerlerle doldurulmuştur; metin sözleşmedeki
# kalıptan üretilir, elle kopyalanmaz. Sunucu adresi yerinde "…" durur:
# varsayılan adresler dokümanda tekrar yazılmaz (KARARLAR 13), kullanıcı
# gerçek adresi ekrandaki iletide görür.
FULL_MESSAGE_SAMPLES = {
    "MSG_OLLAMA_DOWN": {"url": "…"},
    "MSG_OLLAMA_INSTALLED_DOWN": {"url": "…"},
    "MSG_LMSTUDIO_DOWN": {"url": "…"},
    "MSG_LLAMA_MISSING": {},
    "MSG_EMBEDDING_MODEL": {},
    "MSG_EMPTY_OUTPUT": {},
    "MSG_TIMEOUT": {"seconds": defaults.MT_HTTP_TIMEOUT_S},
    "MSG_MLX_ARCH_UNSUPPORTED": {"arch": "gemma4"},
    "MSG_MODEL_NOT_ON_SERVER": {"model": "qwen3:8b", "server": "Ollama"},
    "MSG_REMOTE_REFUSED": {"url": "http://192.168.1.20:11434"},
}

# Dokümandaki örnek kimlikler (önek, değer, openai adresi)
EXAMPLE_IDS = (
    (contracts.PREFIX_MLX, "qwen3-8b-mlx", ""),
    (contracts.PREFIX_GGUF, "Qwen3-8B-Q4_K_M.gguf", ""),
    (contracts.PREFIX_OLLAMA, "qwen3:8b", ""),
    (contracts.PREFIX_LMSTUDIO, "qwen/qwen3-8b", ""),
    (contracts.PREFIX_OPENAI, "qwen", "http://127.0.0.1:8080/v1"),
)

# '*' de adres sonu sayılır: Markdown kalın yazısı (**http://127.0.0.1:8501**)
# adrese yapışıp port ayrıştırmasını bozuyordu (28 Eyl 2026).
URL_RE = re.compile(r"https?://[^\s)\]}\"'`|<>,*]+")
INTERNAL_NAME_RE = re.compile(r"\bYol [AB]\b|\bATLAS\b|\bVECTOR\b")

# Yerel model yolunun kaynak dilini söyleyen ifade: her dokümanda TEK yerde.
# 28 Eylül 2026 (K5, KARARLAR 14): o yer 6 dile güncellendi; eski "yalnız
# İngilizce" ifadesi artık HİÇBİR yerde geçmez (aşağıdaki kalıp 0 kez).
# İngilizceye özgü yinelemeler de geçmez; TeX yolunun dil listesi bu
# kalıplara uymaz.
LOCAL_SOURCE_STATEMENT_RE = re.compile(r"[Yy]alnız(?:ca)? İngilizce")
# Yerel model yolunun dil listesi (tek kaynak: core.lang_detect).
LOCAL_SOURCE_LIST = language_list_tr(TEX_SOURCE_LANGS)
# Gerçek model kalitesinin bu dillerde henüz ölçülmediğini söyleyen not
# (abartısız; her kullanıcı dokümanında).
QUALITY_NOTE = "çeviri kalitesi seçilen modele bağlıdır"
ENGLISH_ONLY_REPEAT_RE = re.compile(
    r"İngilizce (?:bir kitap|kitap|dışı|değil|olmayan)|gerçekten İngilizce")
INTERNAL_BRAND_RE = re.compile(r"\bATLAS\b|\bVECTOR\b")

# Türkçe harfi ASCII'ye düşmüş yaygın sözcükler (düzyazıda; kod dışı).
ASCII_TURKISH = (
    "Turkce", "Türkce", "turkce", "ceviri", "Ceviri", "cevir", "cevirir",
    "icin", "Icin", "degil", "calistir", "calistirin", "calisir", "yukle",
    "yukleyin", "yuklu", "kapali", "acik", "klasoru", "klasorunu", "dosyasi",
    "secin", "secili", "guncel", "ozel", "bellegi", "baglanti", "baslat",
    "baslatin", "sunucuyu baslat", "gosterir", "Kullanilamaz", "kullanilamaz",
    "zeka",
)
ASCII_TURKISH_RE = re.compile(r"\b(?:" + "|".join(ASCII_TURKISH) + r")\b")
TURKISH_LETTERS = set("çğıöşüÇĞİÖŞÜ")


# ---------------------------------------------------------------------------
# Yardımcılar
# ---------------------------------------------------------------------------
def _read(relpath: str) -> str:
    return (PROJECT_ROOT / relpath).read_text(encoding="utf-8")


def _flat(text: str) -> str:
    return " ".join(text.split())


def _tex_plain(text: str) -> str:
    """TeX kaynağını karşılaştırılabilir düz metne indir (yalnız bu belgedeki
    kullanım: \\dosya/\\dugme/\\textbf sarmalayıcıları ve kaçış işaretleri)."""
    # \allowbreak ve ardındaki boşluk bölünme ipucudur, metne ait değildir
    # ("config/\allowbreak defaults.py" -> "config/defaults.py").
    out = re.sub(r"\\allowbreak\s*", "", text).replace("-{}-", "--")
    out = re.sub(r"(?<!\\)~", " ", out)                 # bağlı boşluk
    out = re.sub(r"\\textasciitilde(\{\})?", "~", out)
    out = out.replace("\\ldots{}", "…").replace("\\ldots", "…")
    out = out.replace("\\(\\rightarrow\\)", "→")
    for esc, char in (("\\_", "_"), ("\\#", "#"), ("\\%", "%"), ("\\&", "&"),
                      ("\\{", "{"), ("\\}", "}")):
        out = out.replace(esc, char)
    out = out.replace("\\\\", " ").replace("\\ ", " ")
    wrapper = re.compile(r"\\(?:dosya|dugme|textbf|emph|texttt|textsf)\{([^{}]*)\}")
    while True:
        new = wrapper.sub(r"\1", out)
        if new == out:
            break
        out = new
    return _flat(out)


def _text(relpath: str) -> str:
    """Belgenin karşılaştırılabilir düz hâli (TeX için sadeleştirilmiş)."""
    raw = _read(relpath)
    return _tex_plain(raw) if relpath.endswith(".tex") else _flat(raw)


def _md_section(text: str, start: str, end: str) -> str:
    """``start`` başlığından ``end`` başlığına kadar (ikisi de satır başı)."""
    head = text.split(start, 1)
    assert len(head) == 2, f"bölüm yok: {start!r}"
    return head[1].split(end, 1)[0]


def _md_prose(text: str) -> str:
    """Markdown düzyazısı: kod blokları ve satır içi kod atılır."""
    text = re.sub(r"```.*?```", " ", text, flags=re.S)
    return re.sub(r"`[^`\n]*`", " ", text)


def _tex_prose(text: str) -> str:
    """TeX düzyazısı: yorumlar, komut kutuları, dosya/kod adları atılır."""
    text = re.sub(r"\\begin\{komut\}.*?\\end\{komut\}", " ", text, flags=re.S)
    text = re.sub(r"(?m)(?<!\\)%.*$", " ", text)
    text = re.sub(r"\\(?:dosya|texttt)\{[^{}]*\}", " ", text)
    return text


def _msg_head(template: str) -> str:
    """Kalıbın dokümanda aranacak sabit başı: yer tutucusuz ilk anlamlı parça.

    İlk en az 12 karakterlik düz metin parçası alınır; ilk tümce / iki nokta /
    parantezde kesilir (ör. "Ollama sunucusu kapalı ya da erişilemiyor").
    """
    for literal, *_ in string.Formatter().parse(template):
        piece = re.split(r"\s\(|: |; |\. ", (literal or "").strip(), maxsplit=1)[0]
        piece = piece.strip(" “”\"'.:;(")
        if len(piece) >= 12:
            return piece
    raise AssertionError(f"kalıpta sabit parça yok: {template!r}")


def _json_blocks(text: str) -> list:
    return [json.loads(b) for b in re.findall(r"```json\n(.*?)```", text, re.S)]


def _sources_section() -> str:
    return _md_section(_read(MODEL_DOC), "### 5.1.", "### 5.2.")


def _sources_block() -> dict:
    """§5.1'deki satır içi config/mt_sources.json örneği (tek blok)."""
    blocks = _json_blocks(_sources_section())
    assert len(blocks) == 1, f"§5.1'de tek JSON örneği beklenir: {len(blocks)}"
    assert isinstance(blocks[0], dict)
    return blocks[0]


def _engine_entries() -> dict:
    """§5.2'deki satır içi config/mt_engines.json örneklerinin kayıtları."""
    blocks = _json_blocks(_md_section(_read(MODEL_DOC), "### 5.2.", "## 6."))
    assert blocks, "§5.2'de JSON örneği yok"
    entries: dict = {}
    for block in blocks:
        assert isinstance(block, dict)
        entries.update({k: v for k, v in block.items() if not k.startswith("_")})
    return entries


def _write_json(path: Path, data: dict) -> Path:
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return path


def _table_rows(section: str) -> dict:
    """``| `ad` | ... |`` satırları: ad -> satırın geri kalanı."""
    rows = {}
    for m in re.finditer(r"(?m)^\| `([A-Za-z_][A-Za-z0-9_]*)` \|(.*)$", section):
        rows[m.group(1)] = m.group(2)
    return rows


def _prepare_example_models(models_dir: Path) -> None:
    """Örnek kayıtların gösterdiği modelleri (sahte) yalıtılmış models/'a koy."""
    for entry in _engine_entries().values():
        backend = entry.get("backend", contracts.BACKEND_MLX)
        if backend == contracts.BACKEND_LLAMACPP and entry.get("path"):
            write_fake_gguf(models_dir / entry["path"], "qwen3", size_label="8B")
        elif backend == contracts.BACKEND_MLX:
            folder = entry.get("local_dir") or entry.get("path")
            if folder:
                write_fake_mlx_dir(models_dir / folder, "qwen3")


def _registry_has_resolver() -> bool:
    return hasattr(registry, "resolve_spec")


def _warnings(caplog) -> list:
    return [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]


# ===========================================================================
# llama-cpp-python: isteğe bağlı, tek kurulum komutu
# ===========================================================================
class TestLlamaCppInstall:
    @pytest.mark.parametrize("relpath", [MODEL_DOC, SETUP_DOC, README])
    def test_markdown_docs_contain_exact_command(self, relpath):
        assert contracts.LLAMA_CPP_INSTALL_CMD in _read(relpath), relpath

    def test_tex_contains_exact_command(self):
        assert contracts.LLAMA_CPP_INSTALL_CMD in _text(TEX_DOC)

    def test_tex_command_box_keeps_equals_sign(self):
        # Türkçe polyglossia '=' önüne boşluk ekler; komut kutusu bunu
        # kapatmazsa PDF'den kopyalanan komut bozuk olur. Kutunun seçenekleri
        # dengeli süslü parantezle alınır (kapanış satır sonunda da olabilir;
        # eski desen yalnız "\n}" kapanışını tanıyordu).
        raw = _read(TEX_DOC)
        head = "\\newtcolorbox{komut}{"
        start = raw.find(head)
        assert start >= 0, "komut kutusu tanımı yok"
        depth, pos = 1, start + len(head)
        while depth and pos < len(raw):
            depth += {"{": 1, "}": -1}.get(raw[pos], 0)
            pos += 1
        assert depth == 0, "komut kutusu tanımı kapanmıyor"
        options = raw[start + len(head):pos - 1]
        assert "\\XeTeXinterchartokenstate=0" in options

    def test_missing_message_uses_same_command(self):
        assert contracts.LLAMA_CPP_INSTALL_CMD in contracts.MSG_LLAMA_MISSING

    def test_requirements_only_mentions_it_in_comments(self):
        lines = _read("requirements.txt").splitlines()
        active = [ln for ln in lines if ln.strip() and not ln.lstrip().startswith("#")]
        assert not any("llama" in ln.lower() for ln in active), active
        comments = [ln.lstrip().lstrip("#").strip() for ln in lines
                    if ln.lstrip().startswith("#")]
        assert contracts.LLAMA_CPP_INSTALL_CMD in comments

    @pytest.mark.parametrize("relpath", [MODEL_DOC, SETUP_DOC, TEX_DOC])
    def test_docs_say_optional(self, relpath):
        assert "isteğe bağlı" in _text(relpath).casefold(), relpath


# ===========================================================================
# Kimlik önekleri ve örnek kimlikler
# ===========================================================================
class TestEngineIds:
    @pytest.mark.parametrize("relpath", [MODEL_DOC, SETUP_DOC, TEX_DOC])
    @pytest.mark.parametrize("prefix", contracts.ID_PREFIXES)
    def test_every_prefix_documented(self, relpath, prefix):
        assert f"{prefix}{contracts.ID_PREFIX_SEP}" in _text(relpath), (relpath, prefix)

    @pytest.mark.parametrize("prefix,value,url", EXAMPLE_IDS)
    def test_example_ids_built_by_contract(self, prefix, value, url):
        engine_id = contracts.build_engine_id(prefix, value, url)
        assert engine_id in _read(MODEL_DOC), engine_id

    def test_id_table_covers_all_prefixes_and_default(self):
        section = _md_section(_read(MODEL_DOC), "### 4.1.", "### 4.2.")
        ids = re.findall(r"(?m)^\| `([^`]+)` \|", section)
        prefixes = {i.split(contracts.ID_PREFIX_SEP, 1)[0] for i in ids
                    if contracts.ID_PREFIX_SEP in i}
        assert prefixes == set(contracts.ID_PREFIXES)
        assert registry.DEFAULT_ENGINE in ids

    def test_openai_id_quoted_for_shell(self):
        # '|' kabukta boru işaretidir: örnek komut kimliği tırnak içinde verir.
        engine_id = contracts.build_engine_id(*EXAMPLE_IDS[-1])
        for relpath in (MODEL_DOC, SETUP_DOC):
            assert f"--mt-engine '{engine_id}'" in _read(relpath), relpath

    @pytest.mark.skipif(not _registry_has_resolver(),
                        reason="registry.resolve_spec (K2) henüz yok")
    @pytest.mark.parametrize("prefix,value,url", EXAMPLE_IDS)
    def test_example_ids_resolve(self, prefix, value, url):
        models_dir = Path(registry.MODELS_DIR)
        if prefix == contracts.PREFIX_MLX:
            write_fake_mlx_dir(models_dir / value, "qwen3")
        elif prefix == contracts.PREFIX_GGUF:
            write_fake_gguf(models_dir / value, "qwen3", size_label="8B")
        spec = registry.resolve_spec(contracts.build_engine_id(prefix, value, url))
        assert spec.backend == contracts.PREFIX_BACKEND[prefix]
        if prefix in (contracts.PREFIX_OLLAMA, contracts.PREFIX_LMSTUDIO,
                      contracts.PREFIX_OPENAI):
            assert spec.model == value


# ===========================================================================
# Adresler: yalnız bu bilgisayar, varsayılanlar config/defaults.py'den
# ===========================================================================
def _urls(relpath: str) -> list:
    return URL_RE.findall(_text(relpath))


class TestUrls:
    # KARARLAR 13 (28 Eyl 2026): varsayılan sunucu adresleri dokümanda
    # tekrar yazılmaz; tek kaynak config/defaults.py. Eski sürüm adreslerin
    # dokümanda defaults ile AYNI yazılmasını istiyordu (kararla değişti).
    @pytest.mark.parametrize("relpath", [MODEL_DOC, TEX_DOC])
    def test_default_urls_cited_from_defaults(self, relpath):
        text = _text(relpath)
        assert "config/defaults.py" in text, relpath
        for name in DEFAULT_URL_NAMES:
            assert name in text, (relpath, name)

    @pytest.mark.parametrize("relpath", [SETUP_DOC, README])
    def test_setup_docs_point_to_defaults(self, relpath):
        assert "config/defaults.py" in _text(relpath), relpath

    @pytest.mark.parametrize("relpath", USER_DOCS)
    def test_default_server_addresses_not_repeated(self, relpath):
        text = _text(relpath)
        default_parts = [urllib.parse.urlsplit(url) for url in DEFAULT_URLS]
        default_ports = {parts.port for parts in default_parts}
        for parts in default_parts:
            assert parts.netloc not in text, (relpath, parts.netloc)
        for url in _urls(relpath):
            parts = urllib.parse.urlsplit(url)
            if is_loopback_host(parts.hostname):
                assert parts.port not in default_ports, (relpath, url)

    def test_default_rule_catches_repeat(self):
        # Denetimin kendisi: varsayılan adres geçen bir metin yakalanır.
        sample = f"Sunucu {defaults.OLLAMA_DEFAULT_URL} adresinde dinler."
        parts = urllib.parse.urlsplit(URL_RE.findall(sample)[0])
        assert parts.port == urllib.parse.urlsplit(defaults.OLLAMA_DEFAULT_URL).port

    def test_url_pattern_stops_at_markdown_bold(self):
        assert URL_RE.findall("**http://127.0.0.1:8501** adresinde") == [
            "http://127.0.0.1:8501"]

    @pytest.mark.parametrize("relpath", USER_DOCS)
    def test_server_urls_are_loopback_or_refused_example(self, relpath):
        refused = FULL_MESSAGE_SAMPLES["MSG_REMOTE_REFUSED"]["url"]
        for url in _urls(relpath):
            parts = urllib.parse.urlsplit(url)
            if parts.port is None:
                continue          # web sitesi bağlantısı (ör. ollama.com)
            if url == refused:
                continue          # reddedilen adres örneği (sorun giderme)
            assert check_local_url(url), (relpath, url)

    def test_refused_example_really_refused(self):
        refused = FULL_MESSAGE_SAMPLES["MSG_REMOTE_REFUSED"]["url"]
        with pytest.raises(contracts.BackendUnavailable) as exc:
            check_local_url(refused)
        assert str(exc.value) == contracts.MSG_REMOTE_REFUSED.format(url=refused)

    @pytest.mark.parametrize("relpath", [MODEL_DOC, SETUP_DOC, TEX_DOC])
    def test_loopback_rule_documented(self, relpath):
        text = _text(relpath)
        for host in ("127.0.0.1", "localhost", "::1"):
            assert host in text, (relpath, host)


# ===========================================================================
# Ayar dosyaları: örnek dosya YOK, satır içi kısa örnekler (KARARLAR 13)
# ===========================================================================
class TestNoExampleFiles:
    def test_example_files_removed(self):
        config_dir = PROJECT_ROOT / "config"
        assert sorted(p.name for p in config_dir.glob(f"*{EXAMPLE_SUFFIX}")) == []

    @pytest.mark.parametrize("relpath", [*USER_DOCS, "requirements.txt",
                                         "core/mt_engines/contracts.py"])
    def test_no_example_references(self, relpath):
        assert EXAMPLE_SUFFIX not in _read(relpath), relpath

    def test_model_doc_says_no_example_file(self):
        section = _flat(_md_section(_read(MODEL_DOC), "## 5.", "## 6."))
        assert "Örnek dosya yoktur" in section
        assert "isteğe bağlı" in section and "git dışı" in section


SPEC_FIELDS = {f.name for f in dataclasses.fields(EngineSpec)} - {"name", "source"}
SOURCES_SECTION_KEYS = {"ollama": {"url", "enabled"}, "lmstudio": {"url", "enabled"}}


class TestInlineSourcesJson:
    """§5.1: config/mt_sources.json için satır içi kısa örnek."""

    def test_block_shape_and_local_only(self):
        data = _sources_block()
        assert set(data) <= set(SOURCES_SECTION_KEYS) | {"openai_servers"}
        for key, allowed in SOURCES_SECTION_KEYS.items():
            entry = data.get(key, {})
            assert set(entry) <= allowed, (key, entry)
            if "url" in entry:
                assert check_local_url(entry["url"]) == entry["url"]
        for server in data.get("openai_servers", []):
            assert set(server) == {"name", "url"}
            assert server["name"].strip()
            assert check_local_url(server["url"]) == server["url"]

    def test_no_model_dirs(self):
        # KARARLAR 1: tarama kökü yalnız models/; dosya klasör EKLEYEMEZ.
        assert "model_dirs" not in _read(MODEL_DOC).split("## 6.", 1)[0]
        fields = {f.name for f in dataclasses.fields(contracts.SourcesConfig)}
        assert not any("dir" in name for name in fields)

    def test_field_table_covers_every_config_field(self):
        section = _sources_section()
        for field in dataclasses.fields(contracts.SourcesConfig):
            source, _, key = field.name.partition("_")
            doc_name = (f"{source}.{key}" if source in SOURCES_SECTION_KEYS
                        else field.name)
            assert f"`{doc_name}`" in section, doc_name

    def test_loads_without_warnings_and_keeps_defaults(self, tmp_path, caplog):
        discovery = pytest.importorskip("core.mt_engines.discovery")
        data = _sources_block()
        path = _write_json(tmp_path / contracts.MT_SOURCES_FILENAME, data)
        with caplog.at_level(logging.WARNING):
            cfg = discovery.load_sources(path)
        assert _warnings(caplog) == []
        # Yazılan alanlar okunur; yazılmayanlar varsayılan kalır (dokümandaki söz).
        for source in SOURCES_SECTION_KEYS:
            entry = data.get(source, {})
            default_url = getattr(defaults, f"{source.upper()}_DEFAULT_URL")
            assert getattr(cfg, f"{source}_url") == entry.get("url", default_url)
            assert getattr(cfg, f"{source}_enabled") == entry.get("enabled", True)
        assert [(s.name, s.url) for s in cfg.openai_servers] == [
            (s["name"], s["url"]) for s in data.get("openai_servers", [])]


class TestInlineEnginesJson:
    """§5.2: config/mt_engines.json için satır içi kısa örnekler."""

    @pytest.mark.parametrize("name", sorted(_engine_entries()))
    def test_entry_matches_contract(self, name):
        entry = _engine_entries()[name]
        assert contracts.ENGINE_NAME_RE.match(name) and contracts.ID_PREFIX_SEP not in name
        assert name not in registry.BUILTIN_SPECS
        assert set(entry) <= SPEC_FIELDS, set(entry) - SPEC_FIELDS
        backend = entry.get("backend", contracts.BACKEND_MLX)
        assert backend in contracts.BACKENDS
        assert entry.get("format", contracts.FORMAT_CHAT) in contracts.FORMATS
        assert set(entry.get("options", {})) <= contracts.ENGINE_OPTION_KEYS
        if "base_url" in entry:
            assert check_local_url(entry["base_url"]) == entry["base_url"]
        for key in ("path", "local_dir"):
            if key in entry:
                assert "/" not in entry[key] and ".." not in entry[key], entry[key]
        if backend in (contracts.BACKEND_OLLAMA, contracts.BACKEND_OPENAI):
            assert entry.get("model")
        if backend == contracts.BACKEND_LLAMACPP:
            assert entry["path"].endswith(contracts.GGUF_SUFFIX)

    def test_covers_documented_rare_cases(self):
        # KARARLAR 13: özel MLX modeli (adında HY-MT geçmeyen, biçimi kayıtla
        # belirtilen) ve ince ayarlı bir sunucu modeli örneklenir.
        entries = _engine_entries().values()
        assert any(e.get("backend", contracts.BACKEND_MLX) == contracts.BACKEND_MLX
                   and e.get("local_dir") for e in entries)
        assert any(e.get("options") for e in entries)

    def test_no_experimental_raw_in_example(self):
        # Ham HY-MT uçları deneysel (TASARIM B.8): örnek önermez.
        for entry in _engine_entries().values():
            assert not entry.get("raw")
            if entry.get("backend") in (contracts.BACKEND_OLLAMA,
                                        contracts.BACKEND_OPENAI,
                                        contracts.BACKEND_LLAMACPP):
                assert entry.get("format", contracts.FORMAT_CHAT) == contracts.FORMAT_CHAT

    @pytest.mark.skipif(not _registry_has_resolver(),
                        reason="registry.resolve_spec (K2) henüz yok")
    def test_loads_without_warnings(self, tmp_path, caplog):
        _prepare_example_models(Path(registry.MODELS_DIR))
        entries = _engine_entries()
        path = _write_json(tmp_path / contracts.MT_ENGINES_FILENAME, entries)
        with caplog.at_level(logging.WARNING):
            specs = registry.load_user_specs(path)
        assert _warnings(caplog) == []
        assert set(specs) == set(entries)
        for name, entry in entries.items():
            assert specs[name].backend == entry.get("backend", contracts.BACKEND_MLX)
            assert specs[name].format == entry.get("format", contracts.FORMAT_CHAT)
            assert specs[name].source == contracts.SPEC_SOURCE_USER


# ===========================================================================
# Alan ve seçenek tabloları
# ===========================================================================
def _fields_section() -> str:
    return _md_section(_read(MODEL_DOC), "### 5.2.", "## 6.")


def _options_rows() -> dict:
    section = _fields_section().split("`options` anahtarları", 1)[1]
    return _table_rows(section)


class TestFieldTables:
    def test_documented_fields_exist(self):
        fields_table = _fields_section().split("`options` anahtarları", 1)[0]
        documented = set(_table_rows(fields_table))
        assert documented, "alan tablosu bulunamadı"
        assert documented <= SPEC_FIELDS, documented - SPEC_FIELDS

    def test_example_keys_documented(self):
        fields_table = _fields_section().split("`options` anahtarları", 1)[0]
        documented = set(_table_rows(fields_table))
        used = set().union(*(set(e) for e in _engine_entries().values()))
        assert used <= documented, used - documented

    def test_option_keys_match_contract(self):
        assert set(_options_rows()) == set(contracts.ENGINE_OPTION_KEYS)

    @pytest.mark.parametrize("key,values", [
        ("num_ctx", (str(defaults.MT_CONTEXT_TOKENS),)),
        ("n_ctx", (str(defaults.MT_CONTEXT_TOKENS),)),
        ("keep_alive", (defaults.OLLAMA_KEEP_ALIVE,)),
        ("max_parallel", (str(defaults.MT_HTTP_MAX_PARALLEL),
                          str(defaults.MT_HTTP_MAX_PARALLEL_LIMIT))),
        ("temperature", (f"{defaults.MT_GREEDY_TEMPERATURE:g}",)),
        ("seed", (str(defaults.MT_GREEDY_SEED),)),
        ("repeat_penalty", (repr(defaults.MT_REPEAT_PENALTY),)),
        ("request_timeout_s", (str(defaults.MT_HTTP_TIMEOUT_S),
                               str(defaults.SEGMENT_TIMEOUT_S))),
    ])
    def test_option_defaults_match_constants(self, key, values):
        row = _options_rows()[key]
        for value in values:
            assert re.search(rf"(?<![\d.]){re.escape(value)}(?![\d])", row), (key, value, row)

    def test_timeouts_match_constants(self):
        text = _flat(_read(MODEL_DOC))
        assert f"{defaults.MT_MODEL_LOAD_TIMEOUT_S} sn" in text
        assert f"{defaults.MT_HTTP_TIMEOUT_S} sn" in text
        discovery_s = f"{defaults.DISCOVERY_HTTP_TIMEOUT_S:g}".replace(".", ",")
        assert f"{discovery_s} sn" in text        # Türkçe ondalık virgülü
        assert defaults.MT_HTTP_TIMEOUT_S < defaults.SEGMENT_TIMEOUT_S


# ===========================================================================
# İleti kataloğu: dokümandaki metin sözleşmedeki kalıpla aynı
# ===========================================================================
class TestMessages:
    @pytest.mark.parametrize("name", TROUBLE_MESSAGES)
    def test_troubleshooting_table_covers_message(self, name):
        section = _flat(_md_section(_read(MODEL_DOC), "## 8.", "## 9."))
        head = _msg_head(contracts.MESSAGE_CATALOG[name])
        assert head in section, (name, head)

    @pytest.mark.parametrize("name", sorted(FULL_MESSAGE_SAMPLES))
    def test_full_sample_text_identical(self, name):
        text = getattr(contracts, name).format(**FULL_MESSAGE_SAMPLES[name])
        assert text in _flat(_read(MODEL_DOC)), text

    @pytest.mark.parametrize("relpath", [SETUP_DOC, TEX_DOC])
    @pytest.mark.parametrize("name", ["MSG_OLLAMA_DOWN", "MSG_REMOTE_REFUSED"])
    def test_key_messages_in_other_docs(self, relpath, name):
        head = _msg_head(contracts.MESSAGE_CATALOG[name])
        assert head in _text(relpath), (relpath, head)

    def test_head_helper(self):
        assert _msg_head(contracts.MSG_OLLAMA_DOWN) == (
            "Ollama sunucusu kapalı ya da erişilemiyor")
        assert _msg_head(contracts.MSG_MODEL_NOT_ON_SERVER) == "sunucusunda yok"


class TestBranding:
    """Arayüz metinleri (ui/branding.py, K3) dokümanla aynı; yoksa atlanır."""

    @pytest.mark.parametrize("attr,docs", [
        ("MODEL_SCAN_BUTTON", USER_DOCS),
        ("MODEL_REASONING_NOTE", (MODEL_DOC, SETUP_DOC, TEX_DOC)),
    ])
    def test_label_in_docs(self, attr, docs):
        value = getattr(branding, attr, None)
        if value is None:
            pytest.skip(f"branding.{attr} (K3) henüz yok")
        for relpath in docs:
            assert value in _text(relpath), (attr, relpath)

    def test_unavailable_message_first_sentence(self):
        value = getattr(branding, "MODEL_UNAVAILABLE_MESSAGE", None)
        if value is None:
            pytest.skip("branding.MODEL_UNAVAILABLE_MESSAGE (K3) henüz yok")
        first = value.split(". ", 1)[0].rstrip(".") + "."
        for relpath in (MODEL_DOC, SETUP_DOC, TEX_DOC):
            assert first in _text(relpath), relpath


# ===========================================================================
# Çıkış kodu
# ===========================================================================
class TestExitCode:
    def test_documented_with_constant(self):
        tb = pytest.importorskip("translate_book")
        code = getattr(tb, "EXIT_MODEL_UNAVAILABLE", None)
        if code is None:
            pytest.skip("translate_book.EXIT_MODEL_UNAVAILABLE (K2) henüz yok")
        table = _read(SETUP_DOC).split("`translate_book.py` çıkış kodları:", 1)[1]
        assert re.search(rf"(?m)^\| {code} \|", table)
        for relpath in (MODEL_DOC, TEX_DOC, README):
            assert f"çıkış kodu {code}" in _text(relpath).casefold(), relpath


# ===========================================================================
# KARARLAR: models/ + sembolik bağ, model başına bellek, dil, git dışı dosyalar
# ===========================================================================
class TestDecisions:
    @pytest.mark.parametrize("relpath", [MODEL_DOC, SETUP_DOC, TEX_DOC])
    def test_symlink_into_models(self, relpath):
        text = _text(relpath)
        assert "sembolik bağ" in text
        assert "ln -s" in text and "models/" in text

    @pytest.mark.parametrize("relpath", USER_DOCS)
    def test_memory_per_model(self, relpath):
        assert "model başına ayrı" in _text(relpath).casefold(), relpath

    @pytest.mark.parametrize("relpath", USER_DOCS)
    def test_target_turkish_only(self, relpath):
        low = _text(relpath).casefold()
        assert ("hedef dil" in low and "türkçe" in low), relpath

    def test_source_language_stated(self):
        # K5 (KARARLAR 14): eskiden "kaynak dili yalnız İngilizcedir".
        assert (f"yolunun kaynak dilleri: {LOCAL_SOURCE_LIST}"
                in _text(MODEL_DOC))

    # Görev (28 Eyl 2026): yerel model yolunun kaynak dili her dokümanda TEK
    # yerde söylenir; K5 kaynak dilleri genişletince yalnız o yeri (ve bu
    # testteki ifadeyi) günceller. README / kurulum rehberi / kılavuzda bu yer
    # "İki çeviri yolu" tablosunun "Kaynak dil" hücresi, model dokümanında
    # "Kaynak dil" satırıdır. Diğer yerler dilden bağımsız yazılır. K5
    # sonrasında "yalnız İngilizce" hiçbir yerde kalmaz.
    @pytest.mark.parametrize("relpath", USER_DOCS)
    def test_local_source_language_stated_once(self, relpath):
        text = _text(relpath)
        assert LOCAL_SOURCE_STATEMENT_RE.findall(text) == [], relpath
        assert ENGLISH_ONLY_REPEAT_RE.findall(text) == [], relpath
        assert LOCAL_SOURCE_LIST in text, relpath

    @pytest.mark.parametrize("relpath", USER_DOCS)
    def test_quality_note_once(self, relpath):
        # Gerçek model kalitesi İngilizce dışı dillerde henüz ölçülmedi
        # (KARARLAR 14: "Berke 'şimdi' deyince"); doküman bunu abartmadan söyler.
        assert _text(relpath).count(QUALITY_NOTE) == 1, relpath

    def test_single_statement_rule_catches_repeat(self):
        sample = ("Kaynak dil yalnızca İngilizce. Orta alana İngilizce bir kitap "
                  "yükleyin; kitap gerçekten İngilizceyse devam edin.")
        assert len(LOCAL_SOURCE_STATEMENT_RE.findall(sample)) == 1
        assert len(ENGLISH_ONLY_REPEAT_RE.findall(sample)) == 2
        # Arayüz iletisi alıntısı ve TeX yolunun dil listesi sayılmaz.
        allowed = ("kitap İngilizce görünmüyor; İngilizce, Almanca, Rusça; "
                   "İngilizce-Türkçe sözlük")
        assert not LOCAL_SOURCE_STATEMENT_RE.search(allowed)
        assert not ENGLISH_ONLY_REPEAT_RE.search(allowed)

    def test_thinking_off_documented(self):
        text = _read(MODEL_DOC)
        assert '"think": false' in text
        kwargs = json.dumps(contracts.THINKING_OFF_TEMPLATE_KWARGS)
        assert f"`{kwargs}`" in text

    @pytest.mark.parametrize("filename", CONFIG_FILENAMES)
    def test_config_files_git_ignored_and_documented(self, filename):
        # KARARLAR 13: üç ayar dosyası isteğe bağlı olarak desteklenmeye devam
        # eder ve git dışıdır; örnek (.example) dosyaları yoktur.
        ignored = _read(".gitignore").splitlines()
        assert f"config/{filename}" in ignored
        assert f"config/{filename}{EXAMPLE_SUFFIX}" not in ignored
        assert f"config/{filename}" in _read(MODEL_DOC)

    def test_discovery_cli_documented(self):
        for relpath in (MODEL_DOC, SETUP_DOC, README, TEX_DOC):
            assert "python -m core.mt_engines.discovery" in _text(relpath), relpath
        assert "python -m core.mt_engines.discovery --json" in _read(MODEL_DOC)


# ===========================================================================
# İç adlar ve Türkçe yazım
# ===========================================================================
def _new_sections() -> dict:
    """Bu dalgada yazılan model bölümleri (iç ad denetimi için)."""
    setup = _read(SETUP_DOC)
    tex = _read(TEX_DOC)
    readme = _read(README)
    return {
        "KURULUM §2.3": _md_section(setup, "### 2.3.", "### 2.4."),
        "KURULUM §6": _md_section(setup, "## 6.", "## 7."),
        "README model": _md_section(readme, "**Yerel modelin kaynakları:**",
                                    "**Yerel modelde dil denetimi:**"),
        "Kılavuz model": _md_section(tex, "\\section{Model Kaynakları}",
                                     "\\section{İlerleme"),
    }


class TestNamingAndSpelling:
    def test_no_internal_names_in_model_doc(self):
        assert not INTERNAL_NAME_RE.search(_read(MODEL_DOC))

    @pytest.mark.parametrize("key", sorted(_new_sections()))
    def test_no_internal_names_in_new_sections(self, key):
        assert not INTERNAL_NAME_RE.search(_new_sections()[key]), key

    @pytest.mark.parametrize("relpath", USER_DOCS)
    def test_no_atlas_vector_anywhere(self, relpath):
        assert not INTERNAL_BRAND_RE.search(_read(relpath)), relpath

    @pytest.mark.parametrize("relpath", USER_DOCS)
    def test_no_ascii_turkish_in_prose(self, relpath):
        raw = _read(relpath)
        prose = _tex_prose(raw) if relpath.endswith(".tex") else _md_prose(raw)
        hits = sorted(set(ASCII_TURKISH_RE.findall(prose)))
        assert hits == [], (relpath, hits)

    def test_inline_example_descriptions_are_proper_turkish(self):
        for entry in _engine_entries().values():
            text = entry.get("description", "")
            assert not ASCII_TURKISH_RE.search(text), text

    def test_model_doc_has_turkish_letters(self):
        assert TURKISH_LETTERS <= set(_read(MODEL_DOC)) | {"Ğ"}

    def test_yapay_zeka_spelling(self):
        for relpath in USER_DOCS:
            assert "yapay zeka" not in _read(relpath).casefold().replace("zekâ", ""), relpath
