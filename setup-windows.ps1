# precut 설치 스크립트
#
# 이 파일을 직접 실행하지 말고, 같은 폴더의 "1-설치하기.bat" 을 더블클릭하세요.
# (직접 실행: powershell -ExecutionPolicy Bypass -File .\setup-windows.ps1)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path

$logPath = Join-Path $root "precut-log.txt"
try { Start-Transcript -Path $logPath -Append | Out-Null } catch { }


function Write-Step($message) { Write-Host "`n== $message" -ForegroundColor Cyan }
function Write-Ok($message)   { Write-Host "   $message" -ForegroundColor Green }
function Write-Warn($message) { Write-Host "   $message" -ForegroundColor Yellow }

function Invoke-Setup {
    Set-Location $root
    Write-Host "precut 설치" -ForegroundColor White
    Write-Host "위치: $root" -ForegroundColor DarkGray

    Write-Step "설치 폴더 확인"
    if (-not (Test-Path (Join-Path $root "pyproject.toml"))) {
        throw "이 폴더에 pyproject.toml 이 없습니다. 압축을 덜 풀었거나 폴더가 잘못됐습니다."
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
        $version = ""
        try { $version = (& $candidate.Exe ($candidate.Extra + @("--version")) 2>&1) -join " " } catch { continue }
        if ($version -match "Python (\d+)\.(\d+)") {
            if ([int]$Matches[1] -eq 3 -and [int]$Matches[2] -ge 10) {
                $pythonExe = $candidate.Exe
                $pythonExtra = $candidate.Extra
                Write-Ok $version
                break
            }
            Write-Warn "$version 은 너무 낮습니다 (3.10 이상 필요)"
        }
    }

    if (-not $pythonExe) {
        Write-Host ""
        Write-Host "   https://www.python.org/downloads/ 에서 파이썬을 설치하세요." -ForegroundColor Yellow
        Write-Host "   설치 첫 화면 맨 아래 'Add python.exe to PATH' 를 꼭 체크해야 합니다." -ForegroundColor Yellow
        Write-Host "   설치를 마친 뒤 1-설치하기.bat 을 다시 실행하세요." -ForegroundColor Yellow
        throw "파이썬 3.10 이상을 찾지 못했습니다."
    }

    Write-Step "가상환경 만들기 (.venv)"
    $venvPython = Join-Path $root ".venv\Scripts\python.exe"
    if (Test-Path $venvPython) {
        Write-Ok "이미 있어서 그대로 씁니다"
    } else {
        & $pythonExe ($pythonExtra + @("-m", "venv", ".venv"))
        if (-not (Test-Path $venvPython)) { throw "가상환경 생성에 실패했습니다." }
        Write-Ok "새로 만들었습니다"
    }

    Write-Step "precut 설치 (인터넷 속도에 따라 몇 분 걸립니다)"
    & $venvPython -m pip install --quiet --upgrade pip
    & $venvPython -m pip install --quiet -e ".[all]"
    if ($LASTEXITCODE -ne 0) { throw "패키지 설치에 실패했습니다. 위 오류 메시지를 확인하세요." }
    Write-Ok "설치 완료"

    Write-Step "ffmpeg 확인"
    if (Get-Command ffmpeg -ErrorAction SilentlyContinue) {
        Write-Ok "시스템에 설치된 ffmpeg 사용"
    } else {
        Write-Ok "함께 설치된 ffmpeg 사용"
    }

    Write-Host ""
    Write-Host "설치가 끝났습니다." -ForegroundColor Green
    Write-Host ""
    Write-Host "이제 영상을 처리하려면 같은 폴더의 2-영상처리하기.bat 을 실행하세요." -ForegroundColor White
    Write-Host ""
}

try {
    Invoke-Setup
} catch {
    Write-Host ""
    Write-Host "[오류] $($_.Exception.Message)" -ForegroundColor Red
    Write-Host ""
    Write-Host "이 메시지를 그대로 복사해서 물어보시면 도와드릴 수 있습니다." -ForegroundColor DarkGray
}

Write-Host ""
Write-Host "----------------------------------------------------------" -ForegroundColor DarkGray
Write-Host "위 내용을 확인하세요. 아무 키나 누르면 창이 닫힙니다." -ForegroundColor DarkGray
Write-Host "기록은 이 파일에 남아 있습니다: $logPath" -ForegroundColor DarkGray
try { Stop-Transcript | Out-Null } catch { }
