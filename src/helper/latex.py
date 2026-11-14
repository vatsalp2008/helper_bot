"""Find the TeX toolchain and compile .tex files (used by `helper setup` and the renderer)."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from .errors import HelperError

# Places TeX Live and poppler live on macOS when PATH does not include them.
_EXTRA_BIN_DIRS = ("/Library/TeX/texbin", "/opt/homebrew/bin", "/usr/local/bin")


def find_binary(name: str) -> Path | None:
    found = shutil.which(name)
    if found:
        return Path(found)
    for folder in _EXTRA_BIN_DIRS:
        candidate = Path(folder) / name
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return candidate
    for candidate in sorted(Path("/usr/local/texlive").glob(f"*/bin/*/{name}"), reverse=True):
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return candidate
    return None


def kpsewhich(filename: str) -> Path | None:
    binary = find_binary("kpsewhich")
    if binary is None:
        return None
    proc = subprocess.run([str(binary), filename], capture_output=True, text=True, timeout=30)
    out = proc.stdout.strip()
    return Path(out) if proc.returncode == 0 and out else None


@dataclass
class OverfullBox:
    too_wide_pt: float
    start_line: int  # line in the .tex source
    end_line: int
    context: str  # "paragraph", "alignment", or "detected"


@dataclass
class CompileResult:
    ok: bool
    pdf: Path | None
    log: str
    pages: int | None
    overfull: list[OverfullBox] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


_PAGES_RE = re.compile(r"Output written on .+? \((\d+) pages?,")
_OVERFULL_RE = re.compile(
    r"Overfull \\hbox \(([\d.]+)pt too wide\) "
    r"(?:in (paragraph|alignment) at lines (\d+)--(\d+)|(detected) at line (\d+))"
)
_ERROR_RE = re.compile(r"^(?:! .+|.+:\d+: .+)$", re.MULTILINE)


def parse_log(log: str) -> tuple[int | None, list[OverfullBox], list[str]]:
    pages_match = _PAGES_RE.search(log)
    pages = int(pages_match.group(1)) if pages_match else None
    boxes = []
    for m in _OVERFULL_RE.finditer(log):
        if m.group(2):
            boxes.append(OverfullBox(float(m.group(1)), int(m.group(3)), int(m.group(4)), m.group(2)))
        else:
            line = int(m.group(6))
            boxes.append(OverfullBox(float(m.group(1)), line, line, "detected"))
    errors = [e for e in _ERROR_RE.findall(log) if "Warning" not in e]
    return pages, boxes, errors


def compile_tex(tex_path: Path, out_dir: Path | None = None, engine: str = "pdflatex", timeout: int = 120) -> CompileResult:
    """Compile once in nonstop mode. Long log lines are not wrapped, so the log is easy to parse."""
    binary = find_binary(engine)
    if binary is None:
        raise HelperError(f"{engine} not found. Install TeX Live (https://tug.org/texlive/) or MacTeX.")
    tex_path = tex_path.resolve()
    out_dir = (out_dir or tex_path.parent).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ, max_print_line="100000", error_line="254", half_error_line="238")
    cmd = [
        str(binary),
        "-interaction=nonstopmode",
        "-halt-on-error",
        "-file-line-error",
        f"-output-directory={out_dir}",
        tex_path.name,
    ]
    try:
        proc = subprocess.run(cmd, cwd=tex_path.parent, capture_output=True, text=True, timeout=timeout, env=env)
    except subprocess.TimeoutExpired as exc:
        raise HelperError(f"{engine} timed out after {timeout}s on {tex_path.name}") from exc
    log_file = out_dir / f"{tex_path.stem}.log"
    log = log_file.read_text(encoding="utf-8", errors="replace") if log_file.is_file() else proc.stdout
    pdf = out_dir / f"{tex_path.stem}.pdf"
    pages, boxes, errors = parse_log(log)
    ok = proc.returncode == 0 and pdf.is_file()
    return CompileResult(ok=ok, pdf=pdf if pdf.is_file() else None, log=log, pages=pages, overfull=boxes, errors=errors)
