#!/usr/bin/env bash
# Lançador do JFLP Studio (Arch Linux). Na 1ª execução cria .venv ao lado do script e instala o jflp.
# Uso: ./jflp.sh [argumentos do jflp]   (sem argumentos abre a interface: jflp ui)
set -euo pipefail
root="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
venv="$root/.venv"
jflp="$venv/bin/jflp"

if [ ! -x "$jflp" ]; then
    py=""
    for cand in python3 python; do
        if command -v "$cand" >/dev/null 2>&1 \
           && "$cand" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null; then
            py="$cand"
            break
        fi
    done
    if [ -z "$py" ]; then
        echo "Python 3.11 ou mais novo não encontrado. No Arch: sudo pacman -S python" >&2
        exit 1
    fi
    echo "Criando o ambiente virtual em $venv ..."
    "$py" -m venv "$venv" || { echo "Falha ao criar o .venv (no Arch o módulo venv já vem com o pacote python)." >&2; exit 1; }
    echo "Instalando o JFLP Studio (pip install -e .[full]) ..."
    if ! "$venv/bin/python" -m pip install --disable-pip-version-check -q -e "$root[full]"; then
        echo "O extra [full] falhou; instalando sem ele ..."
        "$venv/bin/python" -m pip install --disable-pip-version-check -q -e "$root"
    fi
fi

export PYTHONUTF8=1
if [ "$#" -eq 0 ]; then
    set -- ui
fi
exec "$jflp" "$@"
