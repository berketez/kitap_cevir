"""Pipeline olay akışı: canlı arayüz için yapılandırılmış (JSONL) olaylar.

Pipeline zaten ``pipeline.log``'a insan-okunur kayıt yazıyor. Masaüstü arayüzün
canlı göstergeleri için aynı kayıtları makine-okunur olaylara çeviren bir
logging handler'ı burada duruyor.

Neden ortam değişkeni: ``translate_book.run_pipeline`` kendi logger kurulumunda
``root_logger.handlers.clear()`` çağırıyor, yani dışarıdan önceden eklenen
handler siliniyor. Handler'ı pipeline'ın kendi kurulum noktasından kurmak
gerekiyor; bunu CLI imzalarını değiştirmeden yapmanın yolu ortam değişkeni:

    KITAPCEVIR_EVENT_LOG=/yol/events.jsonl python translate_book.py kitap.pdf

Ortam değişkeni yoksa hiçbir şey değişmez (varsayılan davranış, testler dahil).

Olay biçimi -- her satır bir JSON nesnesi:
    {"ts": "01:06:11", "kind": "phase",  "phase": "5.5", "title": "..."}
    {"ts": "01:06:22", "kind": "qc",     "score": 0, "issues": 1988, ...}
    {"ts": "01:07:16", "kind": "autofix","iterations": 1, "fixes": 121, ...}
    {"ts": "01:06:12", "kind": "sanitizer", "fixes": 264, ...}
    {"ts": "01:07:36", "kind": "round",  "index": 1, "score": 0, "target": 90}
    {"ts": "01:06:11", "kind": "resume", "message": "..."}
    {"ts": "01:06:09", "kind": "log",    "level": "INFO", "message": "..."}

Tanınmayan her kayıt yine ``log`` olayı olarak geçer: arayüz canlı günlüğü
bundan besler, hiçbir bilgi düşmez.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Any, Iterator

EVENT_LOG_ENV = "KITAPCEVIR_EVENT_LOG"

# Faz olaylarını yazan tek günlükçü: translate_book.run_pipeline'ın
# logging.getLogger("pipeline")'ı (ve alt günlükçüleri "pipeline.*"). Olay
# kaydedicisi kök günlükçüye kurulur; başka modüllerin "Phase N:" diye başlayan
# kayıtları (ör. latex_pipeline.sanitizer'ın "Phase 0.6: Replaced 3 \bar with
# \overline.") faz sayılıyor, arayüzde faz satırını ve sayacı sıfırlıyordu
# (F5-08, 2026-09-29). Onlar "log" olayı olarak geçer, bilgi düşmez.
PHASE_LOGGER = "pipeline"


def is_phase_logger(name: str | None) -> bool:
    """Faz olayı bu günlükçüden gelebilir mi ("pipeline" ya da "pipeline.*")?"""
    return bool(name) and (name == PHASE_LOGGER
                           or name.startswith(PHASE_LOGGER + "."))


# Aynı süreçte ikinci kurulumu engellemek için (run_pipeline birden çok kez
# çağrılabilir; her çağrıda handlers.clear() olduğu için yeniden kurulur).
_INSTALLED_PATHS: set[str] = set()


# ---------------------------------------------------------------------------
# Mesaj tanıyıcılar
# ---------------------------------------------------------------------------

_PHASE = re.compile(r"^\s*Phase\s+([\d.]+)\s*[:\s]\s*(.*)$")

_QC = re.compile(
    r"(?:Initial QC|QC):\s*score=(\d+)/100,\s*issues=(\d+)\s*"
    r"\(critical=(\d+),\s*high=(\d+),\s*medium=(\d+),\s*low=(\d+)\)"
)

_QC_ENGINE = re.compile(
    r"QC Engine:\s*(\d+)\s*issues found,\s*score\s*(\d+)/100"
)

_AUTOFIX = re.compile(
    r"AutoFix:\s*(\d+)\s*iterations,\s*(\d+)\s*fixes,\s*score\s*(\d+)\s*->\s*(\d+),"
    r"\s*converged=(\w+)"
)

_AUTOFIX_ITER = re.compile(r"Iteration\s+(\d+):\s*Applied\s+(\d+)\s*fixes")

_SANITIZER = re.compile(
    r"Sanitizer:\s*(\d+)\s*fixes\s*\(artifacts=(\d+),\s*captions=(\d+),"
    r"\s*headers=(\d+),\s*prompts=(\d+),\s*hrefs=(\d+)\)"
)

_ROUND_START = re.compile(r"Outer iteration\s+(\d+)/(\d+)\s*\(prev score=(-?\d+)\)")

_ROUND_DONE = re.compile(
    r"Outer iteration\s+(\d+)/(\d+):\s*score=(-?\d+)\s*"
    r"\(prev=(-?\d+),\s*target=(\d+)\)"
)

_TRANSLATION_DONE = re.compile(
    r"Translation complete\s*\(([\d.]+)s\):\s*(\d+)\s*total,\s*(\d+)\s*translated"
)

# "Segment 120/2400" ya da "Batch: pages 40-59" biçimli ilerleme kayıtları
_SEGMENT_PROGRESS = re.compile(r"[Ss]egment\s+(\d+)\s*/\s*(\d+)")
# Yerel model çevirisinin (MT fazı) gerçek sayacı: latex_pipeline/translator.py
# her alt parti bitince "[BATCH] Translated 40/472 segments (batch_size=8)"
# yazar (F5-07, 2026-09-29). "Segment N/M" yalnız progress_callback verilince
# basılıyordu; translate_book callback vermediği için yerel model çevirisinin en
# uzun fazında hiç ilerleme olayı yoktu. Her toplu çağrı (başlıklar, gövde,
# altyazılar…) kendi toplamıyla sayar: olay o çağrının sayacıdır, çağrılar
# toplanmaz ve birbirine karışmaz (her olay bir öncekinin yerini alır).
# core/translator.py'nin iç "[BATCH] 3/10 işleniyor" satırı parti sayar,
# segment değil; tanınmaz (iç içe iki sayaç çubuğu oynatırdı).
_BATCH_PROGRESS = re.compile(
    r"\[BATCH\]\s+Translated\s+(\d+)\s*/\s*(\d+)\s+segments\b")
_PAGE_PROGRESS = re.compile(r"pages\s+(\d+)\s*-\s*(\d+)(?:\s*/\s*(\d+))?")

_TERMS = re.compile(r"found\s+(\d+)\s+auto-terms")
_LAYOUT = re.compile(r"Layout detected\s*-*\s*(.*)$")


def classify(message: str) -> dict[str, Any] | None:
    """Bir log mesajını yapılandırılmış olaya çevirir; tanımadıysa None."""
    match = _QC.search(message)
    if match:
        return {
            "kind": "qc",
            "score": int(match.group(1)),
            "issues": int(match.group(2)),
            "critical": int(match.group(3)),
            "high": int(match.group(4)),
            "medium": int(match.group(5)),
            "low": int(match.group(6)),
        }

    match = _AUTOFIX.search(message)
    if match:
        return {
            "kind": "autofix",
            "iterations": int(match.group(1)),
            "fixes": int(match.group(2)),
            "score_before": int(match.group(3)),
            "score_after": int(match.group(4)),
            "converged": match.group(5).lower() == "true",
        }

    match = _AUTOFIX_ITER.search(message)
    if match:
        return {
            "kind": "autofix_iteration",
            "iteration": int(match.group(1)),
            "fixes": int(match.group(2)),
        }

    match = _SANITIZER.search(message)
    if match:
        return {
            "kind": "sanitizer",
            "fixes": int(match.group(1)),
            "artifacts": int(match.group(2)),
            "captions": int(match.group(3)),
            "headers": int(match.group(4)),
            "prompts": int(match.group(5)),
            "hrefs": int(match.group(6)),
        }

    match = _ROUND_DONE.search(message)
    if match:
        return {
            "kind": "round",
            "index": int(match.group(1)),
            "total": int(match.group(2)),
            "score": int(match.group(3)),
            "target": int(match.group(5)),
            "state": "done",
        }

    match = _ROUND_START.search(message)
    if match:
        return {
            "kind": "round",
            "index": int(match.group(1)),
            "total": int(match.group(2)),
            "previous_score": int(match.group(3)),
            "state": "start",
        }

    match = _TRANSLATION_DONE.search(message)
    if match:
        return {
            "kind": "translation_done",
            "seconds": float(match.group(1)),
            "total": int(match.group(2)),
            "translated": int(match.group(3)),
        }

    match = _QC_ENGINE.search(message)
    if match:
        return {
            "kind": "qc_pass",
            "issues": int(match.group(1)),
            "score": int(match.group(2)),
        }

    match = _BATCH_PROGRESS.search(message) or _SEGMENT_PROGRESS.search(message)
    if match:
        return {
            "kind": "progress",
            "unit": "segment",
            "current": int(match.group(1)),
            "total": int(match.group(2)),
        }

    match = _PAGE_PROGRESS.search(message)
    if match:
        event: dict[str, Any] = {
            "kind": "progress",
            "unit": "page",
            "current": int(match.group(2)),
            "from": int(match.group(1)),
        }
        if match.group(3):
            event["total"] = int(match.group(3))
        return event

    match = _PHASE.match(message)
    if match:
        return {
            "kind": "phase",
            "phase": match.group(1),
            "title": match.group(2).strip(),
        }

    match = _TERMS.search(message)
    if match:
        return {"kind": "terms", "count": int(match.group(1))}

    match = _LAYOUT.search(message)
    if match:
        return {"kind": "layout", "detail": match.group(1).strip()}

    if message.startswith("RESUME:"):
        return {"kind": "resume", "message": message[len("RESUME:"):].strip()}

    return None


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------

class JsonlEventHandler(logging.Handler):
    """Her log kaydını JSONL olayına çevirip dosyaya satır satır yazar.

    Yazma hataları pipeline'ı DÜŞÜRMEZ: olay akışı yardımcı bir kanaldır,
    çeviri işinin kendisi ondan bağımsız sürer.
    """

    def __init__(self, path: str | Path, level: int = logging.INFO) -> None:
        super().__init__(level=level)
        self.path = Path(path)
        self._stream = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._stream = self.path.open("a", encoding="utf-8")
        except OSError:
            self._stream = None

    def emit(self, record: logging.LogRecord) -> None:
        if self._stream is None:
            return
        try:
            message = record.getMessage()
        except Exception:  # biçimlendirme hatası olay akışını durdurmasın
            return

        event: dict[str, Any] = {
            "ts": time.strftime("%H:%M:%S", time.localtime(record.created)),
            "epoch": record.created,
            "level": record.levelname,
            "logger": record.name,
        }

        recognized = classify(message)
        if (recognized and recognized.get("kind") == "phase"
                and not is_phase_logger(record.name)):
            recognized = None           # F5-08: yalnız pipeline fazları
        if recognized:
            event.update(recognized)
        else:
            event["kind"] = "log"
        event["message"] = message

        if record.levelno >= logging.ERROR:
            event["kind"] = "error"

        try:
            self._stream.write(json.dumps(event, ensure_ascii=False) + "\n")
            self._stream.flush()
        except (OSError, ValueError):
            pass

    def close(self) -> None:
        try:
            if self._stream is not None:
                self._stream.close()
        except OSError:
            pass
        finally:
            self._stream = None
            super().close()


def install_event_log(path: str | Path | None = None,
                      logger: logging.Logger | None = None) -> Path | None:
    """Olay akışını kurar. Yol verilmezse ortam değişkenine bakar.

    Yol yoksa hiçbir şey yapmaz ve None döner -- pipeline'ın varsayılan
    davranışı böylece hiç değişmez.
    """
    target = path or os.environ.get(EVENT_LOG_ENV)
    if not target:
        return None

    resolved = Path(target).expanduser()
    root = logger or logging.getLogger()

    # Aynı yola ikinci handler eklenmesin (olaylar çiftlenmesin).
    for handler in root.handlers:
        if isinstance(handler, JsonlEventHandler) and handler.path == resolved:
            return resolved

    handler = JsonlEventHandler(resolved)
    if handler._stream is None:      # açılamadıysa sessizce vazgeç
        return None
    root.addHandler(handler)
    _INSTALLED_PATHS.add(str(resolved))
    return resolved


# ---------------------------------------------------------------------------
# Okuyucu (arayüz tarafı)
# ---------------------------------------------------------------------------

def read_events(path: str | Path) -> Iterator[dict[str, Any]]:
    """JSONL olay dosyasını okur. Yarım yazılmış son satır atlanır."""
    file_path = Path(path)
    if not file_path.is_file():
        return
    try:
        content = file_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return
    for line in content.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            yield json.loads(line)
        except json.JSONDecodeError:
            continue
