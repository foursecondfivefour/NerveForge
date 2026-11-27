[CmdletBinding()]
param(
    [switch]$InstallMissing,
    [switch]$InstallTailscale
)

$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path

$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = [Security.Principal.WindowsPrincipal]::new($identity)
if ($principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Warning 'Передатчик и удалённые команды будут работать с правами администратора. Команды через туннель смогут менять системные настройки и файлы других пользователей.'
    $adminConsent = Read-Host 'Для продолжения введите RUN AS ADMIN'
    if ($adminConsent -cne 'RUN AS ADMIN') { throw 'Запуск с правами администратора отменён.' }
}

$pythonCommand = Get-Command python.exe -ErrorAction SilentlyContinue
$pyLauncher = Get-Command py.exe -ErrorAction SilentlyContinue
$cloudflared = Get-Command cloudflared.exe -ErrorAction SilentlyContinue
$tailscale = Get-Command tailscale.exe -ErrorAction SilentlyContinue
$missing = @()
if (-not $pythonCommand -and -not $pyLauncher) { $missing += 'Python 3.10+' }

if ($missing.Count -gt 0) {
    if (-not $InstallMissing) {
        Write-Host "Не найдены: $($missing -join ', ')."
        Write-Host 'Установите Python 3.10+ вручную, затем запустите этот файл снова.'
        Write-Host 'Либо запустите с -InstallMissing: скрипт покажет список пакетов winget и запросит подтверждение.'
        exit 2
    }
    $winget = Get-Command winget.exe -ErrorAction SilentlyContinue
    if (-not $winget) { throw 'winget.exe не найден. Установите Python вручную.' }
    $packages = @()
    if (-not $pythonCommand -and -not $pyLauncher) { $packages += 'Python.Python.3.12' }
    Write-Host "Будут установлены пакеты из winget: $($packages -join ', ')"
    $answer = Read-Host 'Продолжить установку? Введите Y для подтверждения'
    if ($answer -notin @('Y', 'y', 'YES', 'yes')) { throw 'Установка отменена пользователем.' }
    foreach ($package in $packages) {
        & $winget.Source install --id $package --exact --scope user --accept-package-agreements --accept-source-agreements
        if ($LASTEXITCODE -ne 0) { throw "winget завершился с кодом $LASTEXITCODE при установке $package" }
    }
    Write-Host 'Установка завершена. Закройте и откройте PowerShell заново, затем повторите setup_windows.ps1.'
    exit 0
}

if ($InstallTailscale -and -not $tailscale) {
    $winget = Get-Command winget.exe -ErrorAction SilentlyContinue
    if (-not $winget) { throw 'winget.exe не найден. Установите Tailscale вручную с tailscale.com/download/windows.' }
    Write-Host 'Будет установлен пакет из winget: Tailscale.Tailscale'
    $answer = Read-Host 'Продолжить установку? Введите Y для подтверждения'
    if ($answer -notin @('Y', 'y', 'YES', 'yes')) { throw 'Установка отменена пользователем.' }
    & $winget.Source install --id Tailscale.Tailscale --exact --accept-package-agreements --accept-source-agreements
    if ($LASTEXITCODE -ne 0) { throw "winget завершился с кодом $LASTEXITCODE при установке Tailscale" }
    Write-Host 'Tailscale установлен. Войдите в него и повторите запуск setup_windows.ps1.'
    exit 0
}

if ($cloudflared) {
    Write-Host 'Cloudflared найден: будет использован Cloudflare Quick Tunnel (основной режим).'
} elseif ($tailscale) {
    Write-Host 'Cloudflared не найден; доступен резервный режим Tailscale.'
} else {
    Write-Warning 'Клиент туннеля не найден: приложение работает локально. Для публичного доступа установите cloudflared.'
}

if ($pythonCommand) {
    $PythonExe = $pythonCommand.Source
    $PythonPrefix = @()
} else {
    $PythonExe = $pyLauncher.Source
    $PythonPrefix = @('-3')
}
$pythonVersion = & $PythonExe @PythonPrefix -c 'import sys; print(str(sys.version_info.major)+chr(46)+str(sys.version_info.minor))'
if ($LASTEXITCODE -ne 0) { throw 'Не удалось запустить Python.' }
$parts = $pythonVersion.Trim().Split('.')
if ([int]$parts[0] -lt 3 -or ([int]$parts[0] -eq 3 -and [int]$parts[1] -lt 10)) {
    throw "Требуется Python 3.10 или новее; найден $pythonVersion."
}

$currentWorkspace = [Environment]::GetEnvironmentVariable('NERVEFORGE_WORKSPACE', 'User')
if (-not $currentWorkspace) { $currentWorkspace = Join-Path ([Environment]::GetFolderPath('MyDocuments')) 'NerveForgeProjects' }
Write-Host "Рабочая папка сейчас: $currentWorkspace"
Write-Host 'Чтобы Unity импортировал создаваемые .cs-файлы автоматически, укажи папку Assets нужного Unity-проекта.'
$workspaceInput = Read-Host 'Рабочая папка (Enter — оставить текущую)'
if ($workspaceInput.Trim()) { $currentWorkspace = [Environment]::ExpandEnvironmentVariables($workspaceInput.Trim()) }
if (-not [IO.Path]::IsPathRooted($currentWorkspace)) { throw 'Рабочая папка должна быть абсолютным путём.' }
if (-not (Test-Path -LiteralPath $currentWorkspace -PathType Container)) {
    $create = Read-Host "Папки нет. Создать '$currentWorkspace'? Введите Y для подтверждения"
    if ($create -notin @('Y', 'y', 'YES', 'yes')) { throw 'Рабочая папка не выбрана; установка остановлена.' }
    New-Item -ItemType Directory -Path $currentWorkspace -Force | Out-Null
}
$workspaceResolved = (Resolve-Path -LiteralPath $currentWorkspace).Path
[Environment]::SetEnvironmentVariable('NERVEFORGE_WORKSPACE', $workspaceResolved, 'User')
$env:NERVEFORGE_WORKSPACE = $workspaceResolved

Write-Host "NerveForge: $Root"
Write-Host "Python: $pythonVersion"
Write-Host "Workspace: $workspaceResolved"
if ($principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Warning 'NerveForge и все команды удалённого shell будут иметь права администратора. Запускайте только на доверенном тестовом ПК.'
} else {
    Write-Host 'NerveForge работает от имени обычного пользователя; права администратора не используются.'
}
$start = Read-Host 'Запустить передатчик и временный HTTPS-туннель сейчас? [Y/n]'
if ($start -notin @('n', 'N', 'no', 'NO')) {
    Push-Location $Root
    try { & $PythonExe @PythonPrefix (Join-Path $Root 'run.py') }
    finally { Pop-Location }
}
