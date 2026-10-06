"""
Merkezi Loglama Sistemi
Her adımı terminalde gösterir
"""

import logging
import sys
from datetime import datetime
from typing import Optional
from functools import wraps
import time

# Renkli terminal çıktısı
class Colors:
    HEADER = '\033[95m'
    BLUE = '\033[94m'
    CYAN = '\033[96m'
    GREEN = '\033[92m'
    YELLOW = '\033[93m'
    RED = '\033[91m'
    BOLD = '\033[1m'
    UNDERLINE = '\033[4m'
    END = '\033[0m'

class TranslationLogger:
    """Çeviri işlemleri için özel logger"""
    
    _instance = None
    
    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance
    
    def __init__(self):
        if self._initialized:
            return
        self._initialized = True
        self.verbose = True
        self.start_times = {}
        
    def _log(self, level: str, module: str, message: str, color: str = Colors.END):
        """Temel log fonksiyonu"""
        timestamp = datetime.now().strftime("%H:%M:%S.%f")[:-3]
        prefix = f"[{timestamp}] [{module}]"
        
        if level == "ERROR":
            print(f"{Colors.RED}{prefix} ❌ {message}{Colors.END}", flush=True)
        elif level == "WARNING":
            print(f"{Colors.YELLOW}{prefix} ⚠️ {message}{Colors.END}", flush=True)
        elif level == "SUCCESS":
            print(f"{Colors.GREEN}{prefix} ✅ {message}{Colors.END}", flush=True)
        elif level == "INFO":
            print(f"{Colors.CYAN}{prefix} ℹ️ {message}{Colors.END}", flush=True)
        elif level == "DEBUG":
            if self.verbose:
                print(f"{Colors.BLUE}{prefix} 🔍 {message}{Colors.END}", flush=True)
        elif level == "STEP":
            print(f"{Colors.BOLD}{prefix} ▶️ {message}{Colors.END}", flush=True)
        else:
            print(f"{prefix} {message}", flush=True)
    
    # Modül bazlı log metodları
    def glossary(self, message: str, level: str = "INFO"):
        self._log(level, "GLOSSARY", message)
    
    def translator(self, message: str, level: str = "INFO"):
        self._log(level, "TRANSLATOR", message)
    
    def memory(self, message: str, level: str = "INFO"):
        self._log(level, "MEMORY", message)
    
    def parser(self, message: str, level: str = "INFO"):
        self._log(level, "PARSER", message)
    
    def pdf(self, message: str, level: str = "INFO"):
        self._log(level, "PDF", message)
    
    def epub(self, message: str, level: str = "INFO"):
        self._log(level, "EPUB", message)
    
    def ui(self, message: str, level: str = "INFO"):
        self._log(level, "UI", message)
    
    def model(self, message: str, level: str = "INFO"):
        self._log(level, "MODEL", message)
    
    def pipeline(self, message: str, level: str = "INFO"):
        self._log(level, "PIPELINE", message)
    
    def qa(self, message: str, level: str = "INFO"):
        self._log(level, "QA", message)
    
    # Özel log fonksiyonları
    def step(self, step_name: str, details: str = ""):
        """Adım başlangıcı"""
        self.start_times[step_name] = time.time()
        msg = f"BAŞLADI: {step_name}"
        if details:
            msg += f" - {details}"
        self._log("STEP", "STEP", msg)
    
    def step_done(self, step_name: str, result: str = ""):
        """Adım bitişi"""
        elapsed = ""
        if step_name in self.start_times:
            elapsed_sec = time.time() - self.start_times[step_name]
            elapsed = f" ({elapsed_sec:.2f}s)"
            del self.start_times[step_name]
        
        msg = f"BİTTİ: {step_name}{elapsed}"
        if result:
            msg += f" - {result}"
        self._log("SUCCESS", "STEP", msg)
    
    def progress(self, current: int, total: int, item: str = ""):
        """İlerleme göster"""
        pct = (current / total * 100) if total > 0 else 0
        bar_len = 20
        filled = int(bar_len * current / total) if total > 0 else 0
        bar = "█" * filled + "░" * (bar_len - filled)
        msg = f"[{bar}] {current}/{total} ({pct:.1f}%)"
        if item:
            msg += f" - {item[:50]}..."
        print(f"\r{Colors.CYAN}{msg}{Colors.END}", end="", flush=True)
        if current >= total:
            print()  # Yeni satır
    
    def segment(self, idx: int, total: int, source: str, action: str = "çevriliyor"):
        """Segment işleme logu"""
        source_preview = source[:60].replace('\n', ' ') + "..." if len(source) > 60 else source.replace('\n', ' ')
        self._log("DEBUG", "SEGMENT", f"[{idx+1}/{total}] {action}: \"{source_preview}\"")
    
    def term_found(self, term_en: str, term_tr: str, context: str = ""):
        """Bulunan terim"""
        self._log("DEBUG", "TERM", f"'{term_en}' → '{term_tr}'" + (f" (bağlam: {context[:30]}...)" if context else ""))
    
    def cache_hit(self, source: str):
        """Bellekten bulundu"""
        preview = source[:40].replace('\n', ' ') + "..." if len(source) > 40 else source.replace('\n', ' ')
        self._log("DEBUG", "CACHE", f"Bellekten: \"{preview}\"")
    
    def cache_miss(self, source: str):
        """Bellekte yok"""
        preview = source[:40].replace('\n', ' ') + "..." if len(source) > 40 else source.replace('\n', ' ')
        self._log("DEBUG", "CACHE", f"Bellekte yok: \"{preview}\"")
    
    def translation_result(self, source: str, target: str, method: str = "model"):
        """Çeviri sonucu"""
        src_preview = source[:30].replace('\n', ' ') + "..." if len(source) > 30 else source.replace('\n', ' ')
        tgt_preview = target[:30].replace('\n', ' ') + "..." if len(target) > 30 else target.replace('\n', ' ')
        self._log("DEBUG", "TRANSLATION", f"[{method}] \"{src_preview}\" → \"{tgt_preview}\"")
    
    def error(self, module: str, message: str, exception: Exception = None):
        """Hata logu"""
        msg = message
        if exception:
            msg += f" | Exception: {type(exception).__name__}: {str(exception)}"
        self._log("ERROR", module, msg)
    
    def warning(self, module: str, message: str):
        """Uyarı logu"""
        self._log("WARNING", module, message)
    
    def success(self, module: str, message: str):
        """Başarı logu"""
        self._log("SUCCESS", module, message)
    
    def stats(self, stats_dict: dict):
        """İstatistik özeti"""
        print(f"\n{Colors.BOLD}{'='*50}{Colors.END}")
        print(f"{Colors.BOLD}📊 İSTATİSTİKLER{Colors.END}")
        print(f"{Colors.BOLD}{'='*50}{Colors.END}")
        for key, value in stats_dict.items():
            print(f"  {key}: {value}")
        print(f"{Colors.BOLD}{'='*50}{Colors.END}\n")


# Global logger instance
logger = TranslationLogger()


def log_function(module: str = "FUNC"):
    """Fonksiyon dekoratörü - giriş/çıkış loglar"""
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            func_name = func.__name__
            logger._log("DEBUG", module, f"→ {func_name}() çağrıldı")
            start = time.time()
            try:
                result = func(*args, **kwargs)
                elapsed = time.time() - start
                logger._log("DEBUG", module, f"← {func_name}() tamamlandı ({elapsed:.2f}s)")
                return result
            except Exception as e:
                elapsed = time.time() - start
                logger._log("ERROR", module, f"✗ {func_name}() hata ({elapsed:.2f}s): {e}")
                raise
        return wrapper
    return decorator
