# precut 윈도우 설치 스크립트
#
# 사용법: 이 파일을 탐색기에서 우클릭 → "PowerShell에서 실행"
#         (또는 PowerShell에서: powershell -ExecutionPolicy Bypass -File .\setup-windows.ps1)
#
# 스크립트가 자기 위치를 스스로 찾으므로 어느 폴더에서 실행하든 상관없습니다.

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

function Write-Step($message) { Write-Host "`n== $message" -ForegroundColor Cyan }
function Write-Ok($message)   { Write-Host "   $message" -ForegroundColor Green }
function Write-Warn($message) { Write-Host "   $message" -ForegroundColor Yellow }
function Write-Bad($message)  { Write-Host "   $message" -ForegroundColor Red }

function Pause-Exit($code) {
    Write-Host "`n계속하려면 아무 키나 누르세요..." -ForegroundColor DarkGray
    try { $null = $Host.UI.RawUI.ReadKey("NoEcho,IncludeKeyDown") } catch { Read-Host }
    exit $code
}

Write-Host "precut 설치" -ForegroundColor White
Write-Host "위치: $root" -ForegroundColor DarkGray

Write-Step "설치 폴더 확인"
if (-not (Test-Path (Join-Path $root "pyproject.toml"))) {
    Write-Bad "이 폴더에 pyproject.toml 이 없습니다. 압축을 덜 풀었거나 폴더가 잘못됐습니다."
    Pause-Exit 1
}
Write-Ok "정상"

Write-Step "파이썬 확인"
$candidates = @(
    @{ Exe = "py";      Extra = @("-3") },
    @{ Exe = "python";  Extra = @() },
    @{ Exe = "python3"; Extra = @() }
)
$pythonExe = $null
$pythonExtra = @()
foreach ($candidate in $candidates) {
    if (-not (Get-Command $candidate.Exe -ErrorAction SilentlyContinue)) { continue }
    $probeArgs = $candidate.Extra + @("--version")
    $version = ""
    try { $version = (& $candidate.Exe $probeArgs 2>&1) -join " " } catch { continue }
    if ($version -match "Python (\d+)\.(\d+)") {
        if ([int]$Matches[1] -eq 3 -and [int]$Matches[2] -ge 10) {
            $pythonExe = $candidate.Exe
            $pythonExtra = $candidate.Extra
            Write-Ok "$version"
            break
        }
        Write-Warn "$version 은 너무 낮습니다 (3.10 이상 필요)"
    }
}

if (-not $pythonExe) {
    Write-Bad "파이썬 3.10 이상을 찾지 못했습니다."
    Write-Host @"

   https://www.python.org/downloads/ 에서 설치하세요.
   설치 첫 화면 맨 아래 'Add python.exe to PATH' 를 꼭 체크해야 합니다.
   설치 후 이 스크립트를 다시 실행하세요.
"@ -ForegroundColor Yellow
    Pause-Exit 1
}

Write-Step "가상환경 만들기 (.venv)"
$venvPython = Join-Path $root ".venv\Scripts\python.exe"
if (Test-Path $venvPython) {
    Write-Ok "이미 있어서 그대로 씁니다"
} else {
    & $pythonExe ($pythonExtra + @("-m", "venv", ".venv"))
    if (-not (Test-Path $venvPython)) {
        Write-Bad "가상환경 생성에 실패했습니다."
        Pause-Exit 1
    }
    Write-Ok "새로 만들었습니다"
}

Write-Step "precut 설치 (인터넷 속도에 따라 몇 분 걸립니다)"
& $venvPython -m pip install --quiet --upgrade pip
& $venvPython -m pip install --quiet -e ".[all]"
if ($LASTEXITCODE -ne 0) {
    Write-Bad "설치에 실패했습니다. 위 오류 메시지를 확인하세요."
    Pause-Exit 1
}
Write-Ok "설치 완료"

Write-Step "ffmpeg 확인"
if (Get-Command ffmpeg -ErrorAction SilentlyContinue) {
    Write-Ok "시스템에 설치된 ffmpeg 사용"
} else {
    Write-Ok "함께 설치된 ffmpeg 사용"
}

Write-Host @"

설치가 끝났습니다.

앞으로 영상을 처리할 때는 이 폴더의 run-precut.ps1 파일을 우클릭 →
'PowerShell에서 실행' 하면 경로를 물어보고 알아서 처리합니다.

"@ -ForegroundColor White

$answer = Read-Host "지금 바로 영상을 처리할까요? (Y/N)"
if ($answer -match "^[Yy]") {
    & (Join-Path $root "run-precut.ps1")
} else {
    Pause-Exit 0
}
