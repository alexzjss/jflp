# Lançador do JFLP Studio (Windows). Na 1ª execução cria .venv ao lado do script e instala o jflp.
# Uso: .\jflp.ps1 [argumentos do jflp]   (sem argumentos abre a interface: jflp ui)
$ErrorActionPreference = 'Stop'
$root = $PSScriptRoot
$venv = Join-Path $root '.venv'
$jflp = Join-Path $venv 'Scripts\jflp.exe'
$vpy  = Join-Path $venv 'Scripts\python.exe'

function Test-Python311($exe, $pre) {
    # $true se o interpretador existe e é >= 3.11
    try {
        & $exe @pre -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)" 2>$null
        return ($LASTEXITCODE -eq 0)
    } catch { return $false }
}

if (-not (Test-Path $jflp)) {
    $found = $null
    if ((Get-Command py -ErrorAction SilentlyContinue) -and (Test-Python311 'py' @('-3'))) {
        $found = @('py', '-3')
    } elseif ((Get-Command python -ErrorAction SilentlyContinue) -and (Test-Python311 'python' @())) {
        $found = @('python')
    }
    if (-not $found) {
        Write-Error "Python 3.11 ou mais novo não encontrado. Instale em https://www.python.org/downloads/ e tente de novo."
        exit 1
    }
    Write-Host "Criando o ambiente virtual em $venv ..."
    $exe = $found[0]; $pre = @($found | Select-Object -Skip 1)
    & $exe @pre -m venv $venv
    if ($LASTEXITCODE -ne 0) { Write-Error "Falha ao criar o .venv."; exit 1 }
    Write-Host "Instalando o JFLP Studio (pip install -e .[full]) ..."
    & $vpy -m pip install --disable-pip-version-check -q -e "$root[full]"
    if ($LASTEXITCODE -ne 0) {
        Write-Host "O extra [full] falhou; instalando sem ele ..."
        & $vpy -m pip install --disable-pip-version-check -q -e "$root"
        if ($LASTEXITCODE -ne 0) { Write-Error "Falha ao instalar o JFLP Studio."; exit 1 }
    }
}

$env:PYTHONUTF8 = '1'
if ($args.Count -eq 0) { $args = @('ui') }
& $jflp @args
exit $LASTEXITCODE
