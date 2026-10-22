"""App de desktop: janela de app do Edge/Chrome, atalhos, ui.json (instância única) e ui.log (sem console)."""
from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable, Mapping

from .home import Home
from .instance import pid_alive

NOME = "JFLP Studio"
_LINUX_BROWSERS = ("chromium", "chromium-browser", "google-chrome", "microsoft-edge")
_NO_WINDOW = 0x08000000 if os.name == "nt" else 0


def project_root() -> Path:
    """Raiz do repositório/projeto (pasta que contém o pacote `sbfl`)."""
    return Path(__file__).resolve().parent.parent


def icon_path() -> Path:
    return Path(__file__).resolve().parent / "web" / "static" / "jflp.ico"


# ------------------------------------------------------------------ navegador

def find_app_browser(env: Mapping[str, str] | None = None, which: Callable[[str], str | None] = shutil.which,
                     is_file: Callable[[Path], bool] = Path.is_file, windows: bool | None = None) -> str | None:
    """Edge/Chrome (Windows) ou chromium/chrome/edge no PATH; None se não houver."""
    env = os.environ if env is None else env
    if (os.name == "nt") if windows is None else windows:
        bases = [env.get("ProgramFiles"), env.get("ProgramFiles(x86)"), env.get("LOCALAPPDATA")]
        bases = [b for b in bases if b]
        for rel in (r"Microsoft\Edge\Application\msedge.exe", r"Google\Chrome\Application\chrome.exe"):
            for base in bases:
                cand = Path(base) / rel
                if is_file(cand):
                    return str(cand)
        return None
    for name in _LINUX_BROWSERS:
        found = which(name)
        if found:
            return found
    return None


def app_command(browser: str, url: str, profile_dir: Path | str) -> list[str]:
    return [browser, f"--app={url}", f"--user-data-dir={profile_dir}", "--no-first-run",
            "--no-default-browser-check"]


def _profile_in_use(profile_dir: Path | str) -> bool | None:
    """Há processo usando esse user-data-dir? None se não der para saber."""
    needle = str(profile_dir).lower()
    try:
        if os.name == "nt":
            ps = ("Get-CimInstance Win32_Process | Where-Object { $_.CommandLine } | "
                  "ForEach-Object { $_.CommandLine }")
            out = subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True,
                                 text=True, encoding="utf-8", errors="replace", timeout=20,
                                 creationflags=_NO_WINDOW).stdout
            return any("--user-data-dir" in ln.lower() and needle in ln.lower() for ln in out.splitlines())
        for p in Path("/proc").iterdir():
            if p.name.isdigit() and p.name != str(os.getpid()):
                try:
                    cmd = (p / "cmdline").read_bytes().decode("utf-8", "replace").replace("\0", " ").lower()
                except OSError:
                    continue
                if "--user-data-dir" in cmd and needle in cmd:
                    return True
        return False
    except Exception:
        return None


def wait_for_window(popen: subprocess.Popen, profile_dir: Path | str, keep_going: Callable[[], bool],
                    quick: float = 2.0, poll: float = 0.5, in_use=_profile_in_use,
                    settle: float = 3.0) -> None:
    """Bloqueia até a janela de app fechar (ou `keep_going()` virar False; Ctrl+C sobe ao chamador).

    Se o processo lançado sai logo com código 0 (repassou a outro processo), espera enquanto houver
    processos usando o user-data-dir; se isso não puder ser descoberto, segue até `keep_going()` ser False."""
    t0 = time.monotonic()
    while keep_going():
        if popen.poll() is not None:
            break
        time.sleep(poll)
    else:
        return
    if popen.returncode != 0 or time.monotonic() - t0 >= quick:
        return
    # repasse: o navegador real é outro processo
    time.sleep(settle)
    if in_use(profile_dir) is None:
        print("não foi possível saber quando a janela fecha; Ctrl+C encerra o servidor.")
        while keep_going():
            time.sleep(poll)
        return
    gone = 0
    while keep_going():
        gone = 0 if in_use(profile_dir) else gone + 1
        if gone >= 2:
            return
        time.sleep(max(poll, 1.0))


def open_app_window(url: str, home: Home) -> subprocess.Popen | None:
    """Abre a janela de app (sem esperar). None se não houver navegador compatível."""
    browser = find_app_browser()
    if not browser:
        return None
    profile = Path(home.root) / "navegador"
    profile.mkdir(parents=True, exist_ok=True)
    return subprocess.Popen(app_command(browser, url, profile), stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


# ------------------------------------------------------- sem console e ui.json

def ensure_streams(home: Home) -> None:
    """Sem console (pythonw): stdout/stderr são None; manda tudo para <home>/ui.log."""
    if sys.stdout is not None and sys.stderr is not None:
        return
    Path(home.root).mkdir(parents=True, exist_ok=True)
    log = open(Path(home.root) / "ui.log", "a", encoding="utf-8", buffering=1)
    if sys.stdout is None:
        sys.stdout = log
    if sys.stderr is None:
        sys.stderr = log


def ui_json(home: Home) -> Path:
    return Path(home.root) / "ui.json"


def write_ui_state(home: Home, url: str) -> None:
    ui_json(home).write_text(json.dumps({"url": url, "pid": os.getpid()}), encoding="utf-8")


def remove_ui_state(home: Home) -> None:
    """Apaga o ui.json se for desta instância."""
    try:
        data = json.loads(ui_json(home).read_text(encoding="utf-8"))
        if data.get("pid") == os.getpid():
            ui_json(home).unlink()
    except (OSError, ValueError, AttributeError):
        pass


def url_responds(url: str, timeout: float = 2.0) -> bool:
    """Qualquer resposta HTTP (até 4xx) indica que o servidor está de pé."""
    try:
        urllib.request.urlopen(url, timeout=timeout).close()
        return True
    except urllib.error.HTTPError:
        return True
    except Exception:
        return False


def running_instance(home: Home, responds: Callable[[str], bool] = url_responds,
                     alive: Callable[[int | None], bool] = pid_alive) -> str | None:
    """URL da instância já em execução (ui.json com pid vivo e URL respondendo) ou None."""
    try:
        data = json.loads(ui_json(home).read_text(encoding="utf-8"))
        url, pid = data["url"], data["pid"]
    except (OSError, ValueError, KeyError, TypeError):
        return None
    if not isinstance(url, str) or pid == os.getpid() or not alive(pid) or not responds(url):
        return None
    return url


# -------------------------------------------------------------------- atalhos

def _ps(s: str) -> str:
    """Literal de string do PowerShell entre aspas simples (aspas simples dobradas)."""
    return "'" + s.replace("'", "''") + "'"


def _target() -> str:
    exe = Path(sys.executable)
    w = exe.with_name("pythonw.exe")
    return str(w if w.exists() else exe)


_PS_HEAD = "$ErrorActionPreference='Stop'\n[Console]::OutputEncoding=[Text.Encoding]::UTF8\n"
_PS_PLACES = ("$dirs=@([Environment]::GetFolderPath('Desktop'),[Environment]::GetFolderPath('Programs'))\n"
              f"$nome={_ps(NOME + '.lnk')}\n")


def powershell_create_script(target: str, workdir: str, icon: str) -> str:
    return (_PS_HEAD + _PS_PLACES
            + f"$alvo={_ps(target)}\n$pasta={_ps(workdir)}\n$icone={_ps(icon)}\n"
            + "$sh=New-Object -ComObject WScript.Shell\n"
              "foreach($d in $dirs){ if(-not $d){continue}\n"
              "  New-Item -ItemType Directory -Force -Path $d | Out-Null\n"
              "  $p=Join-Path $d $nome\n"
              "  $l=$sh.CreateShortcut($p)\n"
              "  $l.TargetPath=$alvo\n"
              "  $l.Arguments='-m sbfl ui --app'\n"
              "  $l.WorkingDirectory=$pasta\n"
              "  $l.IconLocation=$icone\n"
              f"  $l.Description={_ps(NOME)}\n"
              "  $l.Save()\n"
              "  Write-Output $p }\n")


def powershell_remove_script() -> str:
    return (_PS_HEAD + _PS_PLACES
            + "foreach($d in $dirs){ if(-not $d){continue}\n"
              "  $p=Join-Path $d $nome\n"
              "  if(Test-Path -LiteralPath $p){ Remove-Item -LiteralPath $p -Force; Write-Output $p } }\n")


def _run_ps(script: str) -> list[str]:
    # -EncodedCommand (UTF-16LE em base64): nenhum caminho passa pela linha de comando
    enc = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-EncodedCommand", enc],
                       capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60,
                       creationflags=_NO_WINDOW)
    if r.returncode != 0:
        raise OSError((r.stderr or r.stdout).strip() or "falha no PowerShell")
    return [ln.strip() for ln in r.stdout.splitlines() if ln.strip()]


def desktop_entry_path() -> Path:
    return Path.home() / ".local" / "share" / "applications" / "jflp.desktop"


def desktop_entry(python: str, workdir: str, icon: str) -> str:
    def q(s: str) -> str:      # aspas do campo Exec
        return '"' + s.replace("\\", "\\\\").replace('"', '\\"').replace("$", "\\$").replace("`", "\\`") + '"'
    return ("[Desktop Entry]\nType=Application\n"
            f"Name={NOME}\nComment=Localização de falhas com Jaguar\n"
            f"Exec={q(python)} -m sbfl ui --app\nPath={workdir}\nIcon={icon}\nTerminal=false\n"
            "Categories=Development;\n")


def create_shortcuts() -> list[str]:
    """Cria os atalhos e devolve os caminhos criados."""
    root = str(project_root())
    if os.name == "nt":
        return _run_ps(powershell_create_script(_target(), root, str(icon_path())))
    path = desktop_entry_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(desktop_entry(sys.executable, root, str(icon_path().with_name("favicon.png"))),
                    encoding="utf-8")
    return [str(path)]


def remove_shortcuts() -> list[str]:
    """Remove os atalhos e devolve os caminhos removidos."""
    if os.name == "nt":
        return _run_ps(powershell_remove_script())
    path = desktop_entry_path()
    if path.exists():
        path.unlink()
        return [str(path)]
    return []
