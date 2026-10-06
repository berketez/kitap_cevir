"""
XeLaTeX compiler with multi-pass support and auto-fix.

Runs XeLaTeX in nonstopmode up to N passes (default 3) so that
cross-references and TOC converge.  After each pass the log is
parsed for errors and warnings.  If auto_fix_errors is enabled,
common mistakes (unmatched braces, undefined commands) are patched
and compilation is retried.

Usage:
    compiler = LatexCompiler(config)
    result = compiler.compile(Path("output/source/book.tex"))
    if result.success:
        print("PDF at:", result.pdf_path)
    else:
        for err in result.errors:
            print("ERROR:", err)
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

from latex_pipeline.config import PipelineConfig
from latex_pipeline.inspector import match_file_line_error, unwrap_tex_log

logger = logging.getLogger(__name__)

# R5 (2026-09-25): TeX günlük satırlarını varsayılan olarak 79 karakterde
# keser; uzun çıktı yolunda "-file-line-error" hata satırı yolun ortasından
# bölünüyor, ileti ve yazıtipi adları yarım kalıyordu. kpathsea texmf.cnf
# değişkenlerini ortamdan da okur: derleme bu sınırla koşar. (PDF'ye etkisi
# yoktur; yalnız .log satır kırpmasını kapatır.)
TEX_LOG_MAX_PRINT_LINE = 10000

# Derleyici PATH'te yoksa bakılan macOS TeX yerleri (packaging/TASARIM.md §F,
# F2): önce MacTeX'in sürümden bağımsız bağı, sonra TeX Live'ın yıllık
# kurulumları, en yeni sürüm önce (eskiden yalnız 2025 ve 2024 yazılıydı;
# 2026 kurulunca bulunmuyordu). Komut satırı kullanımı içindir: paketli
# kurulumda PATH'i yönetici kurar.
MACTEX_BIN_DIR = Path("/Library/TeX/texbin")
TEXLIVE_ROOT = Path("/usr/local/texlive")
TEXLIVE_BIN_SUBDIR = "bin/universal-darwin"


def _texlive_version_key(install_dir: Path) -> tuple:
    """TeX Live kurulum klasörünün sıralama anahtarı ("2026" -> (1, (2026,), …)).

    Sayılar tam sayı olarak karşılaştırılır (sözcük sırası değil); sayı
    taşımayan klasör (``texmf-local`` gibi) en sona düşer.
    """
    numbers = tuple(int(part) for part in re.findall(r"\d+", install_dir.name))
    return (1 if numbers else 0, numbers, install_dir.name)


def texlive_candidates(name: str) -> List[Path]:
    """``<TEXLIVE_ROOT>/*/bin/universal-darwin/<name>``: sürümler azalan sırada."""
    found = TEXLIVE_ROOT.glob(f"*/{TEXLIVE_BIN_SUBDIR}/{name}")
    # .../<sürüm>/bin/universal-darwin/<name>: sürüm klasörü üç üstte.
    return sorted(found, key=lambda path: _texlive_version_key(path.parents[2]),
                  reverse=True)


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class CompileResult:
    """Outcome of a LaTeX compilation attempt."""

    success: bool = False
    pdf_path: Optional[Path] = None
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    log_path: Optional[Path] = None
    passes_run: int = 0


# ---------------------------------------------------------------------------
# LatexCompiler
# ---------------------------------------------------------------------------

class LatexCompiler:
    """
    Compile a ``.tex`` file to PDF via XeLaTeX (or LuaLaTeX).

    The compiler:
    1. Runs up to ``config.compile_passes`` passes.
    2. Parses the log after each pass.
    3. If auto-fix is enabled and fixable errors are found, patches the
       ``.tex`` source and reruns.
    4. Copies the final PDF to ``config.compiled_dir``.
    """

    def __init__(self, config: PipelineConfig):
        self.config = config
        self._compiler_bin = self._find_compiler()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def compile(
        self,
        tex_path: str | Path,
        output_dir: Optional[str | Path] = None,
    ) -> CompileResult:
        """
        Compile *tex_path* to PDF.

        Parameters
        ----------
        tex_path   : path to the ``.tex`` file.
        output_dir : directory for the final PDF.  Defaults to
                     ``config.compiled_dir``.

        Returns
        -------
        CompileResult with success flag, pdf_path, errors, warnings, log_path.
        """
        tex_path = Path(tex_path)
        if not tex_path.exists():
            return CompileResult(
                success=False,
                errors=[f"TeX file not found: {tex_path}"],
            )

        output_dir = Path(output_dir) if output_dir else self.config.compiled_dir
        output_dir.mkdir(parents=True, exist_ok=True)

        result = CompileResult()
        retries = 0
        max_retries = self.config.max_compile_retries if self.config.auto_fix_errors else 0

        while retries <= max_retries:
            result = self._run_passes(tex_path)

            if result.success:
                # Copy PDF to output dir
                src_pdf = tex_path.with_suffix(".pdf")
                if src_pdf.exists():
                    dest_pdf = output_dir / src_pdf.name
                    shutil.copy2(src_pdf, dest_pdf)
                    result.pdf_path = dest_pdf
                    logger.info("PDF copied to %s", dest_pdf)
                break

            # Try auto-fix if enabled
            if self.config.auto_fix_errors and result.errors:
                fixed = self._auto_fix(tex_path, result.errors)
                if fixed:
                    retries += 1
                    logger.info("Auto-fix applied (attempt %d/%d), retrying ...",
                                retries, max_retries)
                    continue
            break

        return result

    # ------------------------------------------------------------------
    # Multi-pass compilation
    # ------------------------------------------------------------------

    def _run_passes(self, tex_path: Path) -> CompileResult:
        """Run N passes of XeLaTeX and collect errors/warnings."""
        n_passes = self.config.compile_passes
        all_errors: List[str] = []
        all_warnings: List[str] = []
        log_path: Optional[Path] = None

        # Freshness guard: remove stale artifacts from a previous run so that
        # "PDF exists" below can only mean THIS run produced it, and a stale
        # .log can never be parsed as this run's diagnostics. Without this,
        # a missing compiler binary + leftover PDF reported success=True.
        pdf_path = tex_path.with_suffix(".pdf")
        for stale in (pdf_path, tex_path.with_suffix(".log")):
            try:
                stale.unlink(missing_ok=True)
            except OSError as exc:
                logger.warning("Could not remove stale artifact %s: %s", stale, exc)

        returncode = 0
        for pass_num in range(1, n_passes + 1):
            logger.info("Compilation pass %d/%d: %s", pass_num, n_passes, tex_path.name)
            returncode, log_path = self._run_xelatex(tex_path)
            errors, warnings = self._parse_log(log_path)

            all_errors = errors
            all_warnings = warnings

            if returncode == -1:
                # Compiler never ran / was killed (missing binary, timeout):
                # further passes would only re-parse the same state.
                all_errors.append(
                    f"Compiler failed to run (missing binary or timeout): "
                    f"{self._compiler_bin}"
                )
                break

            if errors:
                logger.warning("Pass %d: %d error(s) found", pass_num, len(errors))
                # If fatal errors, stop early (no point running more passes)
                if self._has_fatal_errors(errors):
                    break

        # Check if PDF was produced (must be >1KB to be valid, not just a
        # header). The stale artifacts were deleted above, so an existing PDF
        # here was written by this run. A killed/absent compiler (returncode
        # -1) is never a success even if a partial PDF was left behind.
        success = (
            returncode != -1
            and pdf_path.exists()
            and pdf_path.stat().st_size > 1024
        )

        return CompileResult(
            success=success,
            pdf_path=pdf_path if success else None,
            errors=all_errors,
            warnings=all_warnings,
            log_path=log_path,
            passes_run=n_passes,
        )

    def _run_xelatex(self, tex_path: Path) -> tuple[int, Optional[Path]]:
        """
        Execute a single XeLaTeX pass.

        Returns (returncode, log_path).
        """
        # Resolve to absolute paths to avoid relative-path confusion
        tex_abs = tex_path.resolve()
        cmd = [
            self._compiler_bin,
            "-interaction=nonstopmode",
            "-file-line-error",
            "-no-shell-escape",
            f"-output-directory={tex_abs.parent}",
            str(tex_abs),
        ]

        logger.debug("Running: %s", " ".join(cmd))

        timeout = getattr(self.config, 'compile_timeout', 600)
        env = {**os.environ, "max_print_line": str(TEX_LOG_MAX_PRINT_LINE)}
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout,
                cwd=str(tex_abs.parent),
                env=env,
            )
            returncode = proc.returncode
        except subprocess.TimeoutExpired:
            logger.error("XeLaTeX timed out after %ds", timeout)
            returncode = -1
        except FileNotFoundError:
            logger.error("Compiler not found: %s", self._compiler_bin)
            returncode = -1

        log_path = tex_abs.with_suffix(".log")
        return returncode, log_path if log_path.exists() else None

    # ------------------------------------------------------------------
    # Log parsing
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_log(log_path: Optional[Path]) -> tuple[List[str], List[str]]:
        """
        Parse a XeLaTeX log file for errors and warnings.

        Returns (errors, warnings).
        """
        if log_path is None or not log_path.exists():
            return ["Log file not found"], []

        try:
            log_text = log_path.read_text(encoding="utf-8", errors="replace")
        except Exception as exc:
            return [f"Cannot read log: {exc}"], []

        errors: List[str] = []
        warnings: List[str] = []

        # R5: kesilmiş (79 sütun) eski günlüklerde parçalar birleştirilir;
        # compiler'ın kendi koşuları zaten kesilmemiş günlük üretir.
        log_lines = unwrap_tex_log(log_text)
        # "[l.N]" yalnız ana .tex'in satırına verilir: başka dosyadaki
        # (.sty/.aux) hatanın satır numarası _auto_fix'i yanlış satıra
        # yönlendirmesin.
        main_tex = log_path.with_suffix(".tex").name
        for i, line in enumerate(log_lines):
            line_stripped = line.strip()

            # Errors -- three formats:
            # 1) Standard: "! Error message" followed by "l.NUM" within 6 lines
            # 2) -file-line-error: "filename:NUM: LaTeX Error: message"
            # 3) -file-line-error, TeX primitive error (R5): "filename:NUM:
            #    Double superscript." — eskiden hiç yakalanmıyordu.
            if line_stripped.startswith("!"):
                # Look ahead up to 6 lines for `l.NUM` line reference
                error_msg = line_stripped
                for lookahead in range(1, 7):
                    if i + lookahead < len(log_lines):
                        next_line = log_lines[i + lookahead].strip()
                        l_match = re.match(r"l\.(\d+)\b", next_line)
                        if l_match:
                            error_msg = f"{line_stripped} [l.{l_match.group(1)}]"
                            break
                errors.append(error_msg)
            elif re.search(r":(\d+):\s*LaTeX\s+Error:", line_stripped):
                # -file-line-error format: "path/file.tex:216: LaTeX Error: ..."
                fle_match = re.search(r":(\d+):\s*LaTeX\s+Error:\s*(.*)", line_stripped)
                if fle_match:
                    errors.append(f"! {fle_match.group(2).strip()} [l.{fle_match.group(1)}]")
                else:
                    errors.append(line_stripped)
            elif (fle := match_file_line_error(line_stripped)) is not None:
                src_file, src_line, msg = fle
                src_name = Path(src_file).name
                if src_name == main_tex:
                    errors.append(f"! {msg} [l.{src_line}]")
                else:
                    errors.append(f"! {msg} [{src_name}:{src_line}]")
            elif ":error:" in line_stripped.lower():
                errors.append(line_stripped)

            # Warnings
            if "warning" in line_stripped.lower() and not line_stripped.startswith("!"):
                warnings.append(line_stripped)
            elif "underfull" in line_stripped.lower() or "overfull" in line_stripped.lower():
                warnings.append(line_stripped)

        # Deduplicate
        errors = list(dict.fromkeys(errors))
        warnings = list(dict.fromkeys(warnings))

        return errors, warnings

    @staticmethod
    def _has_fatal_errors(errors: List[str]) -> bool:
        """Check if errors are unrecoverable (missing files, emergency stop)."""
        fatal_patterns = [
            "emergency stop",
            "fatal error",
            "no such file",
            "xdvipdfmx:fatal",
            "no output pdf",
            "image inclusion failed",
        ]
        for err in errors:
            err_lower = err.lower()
            for pat in fatal_patterns:
                if pat in err_lower:
                    return True
        return False

    # ------------------------------------------------------------------
    # Auto-fix
    # ------------------------------------------------------------------

    def _auto_fix(self, tex_path: Path, errors: List[str]) -> bool:
        """
        Try to fix common LaTeX errors in-place.

        Returns True if any fix was applied.
        """
        try:
            source = tex_path.read_text(encoding="utf-8")
        except Exception:
            return False

        original = source
        fix_applied = False

        for err in errors:
            # Fix 1: Missing } -- add closing brace at end of problematic line
            if "missing" in err.lower() and "}" in err:
                # Try to find the line referenced in the error
                # Format: "! Missing } inserted. [l.81]" or "! Missing } ... l.81"
                line_match = re.search(r"\[?l\.(\d+)\]?", err)
                if line_match:
                    target_line = int(line_match.group(1))
                    lines = source.split("\n")
                    if 0 < target_line <= len(lines):
                        line = lines[target_line - 1]
                        open_count = line.count("{") - line.count("}")
                        if open_count > 0:
                            lines[target_line - 1] = line + "}" * open_count
                            source = "\n".join(lines)
                            fix_applied = True
                            logger.info("Auto-fix: added %d closing brace(s) at line %d",
                                        open_count, target_line)

            # Fix 2: Undefined control sequence -- comment it out
            if "undefined control sequence" in err.lower():
                # Extract the undefined command from the error message
                cmd_match = re.search(r"\\(\w+)", err)
                if cmd_match:
                    bad_cmd = cmd_match.group(0)
                    # Only remove if it's NOT a standard LaTeX command.
                    # Keep this allowlist conservative: commenting out a
                    # real command mid-document can cascade many errors.
                    standard = {
                        # Structural
                        "\\begin", "\\end", "\\section", "\\chapter",
                        "\\subsection", "\\subsubsection", "\\paragraph",
                        "\\part",
                        "\\mainmatter", "\\frontmatter", "\\backmatter",
                        # Text formatting
                        "\\textbf", "\\textit", "\\emph",
                        "\\textcolor", "\\underline",
                        # Figures / floats
                        "\\includegraphics", "\\caption", "\\centering",
                        "\\hspace", "\\vspace", "\\noindent",
                        # Lists
                        "\\item",
                        # Cross-refs
                        "\\label", "\\ref", "\\eqref", "\\pageref",
                        "\\cref", "\\Cref", "\\autoref", "\\nameref",
                        # Citations
                        "\\cite", "\\citet", "\\citep", "\\fullcite",
                        "\\textcite",
                        # Footnotes
                        "\\footnote", "\\footnotetext", "\\footnotemark",
                        # Hyperlinks
                        "\\href", "\\url",
                        # Paragraph control
                        "\\par",
                    }
                    if bad_cmd not in standard:
                        # Comment out lines containing the bad command
                        lines = source.split("\n")
                        for k, line in enumerate(lines):
                            if bad_cmd in line and not line.strip().startswith("%"):
                                lines[k] = "% AUTO-FIX: " + line
                                fix_applied = True
                                logger.info("Auto-fix: commented out '%s' at line %d",
                                            bad_cmd, k + 1)
                        source = "\n".join(lines)

            # Fix 3: Missing image file -- try alternative extensions
            # Error: "! Unable to load picture or PDF file 'name.jpeg'"
            if "unable to load picture" in err.lower():
                img_match = re.search(r"['\"]([^'\"]+\.\w+)['\"]", err)
                if img_match:
                    bad_ref = img_match.group(1)
                    bad_stem = Path(bad_ref).stem
                    # Try to find the actual file in images/ dir
                    img_dir = tex_path.parent / "images"
                    if img_dir.exists():
                        for ext in [".png", ".jpg", ".jpeg", ".pdf", ".svg"]:
                            candidate = bad_stem + ext
                            if (img_dir / candidate).exists() and candidate != bad_ref:
                                # Replace in source
                                source = source.replace(
                                    "{" + bad_ref + "}",
                                    "{" + candidate + "}",
                                )
                                fix_applied = True
                                logger.info(
                                    "Auto-fix: image ref %s -> %s",
                                    bad_ref, candidate,
                                )
                                break

            # Fix 4: Extra } -- remove the stray brace
            if "extra }" in err.lower() or "too many" in err.lower():
                line_match = re.search(r"l\.(\d+)", err)
                if line_match:
                    target_line = int(line_match.group(1))
                    lines = source.split("\n")
                    if 0 < target_line <= len(lines):
                        line = lines[target_line - 1]
                        close_count = line.count("}") - line.count("{")
                        if close_count > 0:
                            # Remove one trailing }
                            idx = line.rfind("}")
                            lines[target_line - 1] = line[:idx] + line[idx + 1:]
                            source = "\n".join(lines)
                            fix_applied = True
                            logger.info("Auto-fix: removed extra } at line %d", target_line)

        if fix_applied and source != original:
            tex_path.write_text(source, encoding="utf-8")
            logger.info("Auto-fix: %s rewritten", tex_path.name)
            return True

        return False

    # ------------------------------------------------------------------
    # Compiler detection
    # ------------------------------------------------------------------

    def _find_compiler(self) -> str:
        """Find the XeLaTeX (or LuaLaTeX) binary."""
        name = self.config.compiler  # "xelatex" or "lualatex"

        # Check PATH
        found = shutil.which(name)
        if found:
            return found

        # macOS TeX yerleri (F2): MacTeX bağı, sonra TeX Live sürümleri
        # (en yenisi önce).
        candidates = [MACTEX_BIN_DIR / name, *texlive_candidates(name)]
        for candidate in candidates:
            if candidate.exists():
                return str(candidate)

        logger.warning("Compiler '%s' not found in PATH; using name as-is", name)
        return name
