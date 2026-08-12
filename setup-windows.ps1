# precut 윈도우 설치 스크립트
#
# 사용법: 이 저장소 폴더에서 PowerShell을 열고
#   powershell -ExecutionPolicy Bypass -File .\setup-windows.ps1
#
# 파이썬 가상환경을 만들고 precut과 필요한 패키지를 모두 설치합니다.

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

function Write-Step($message) { Write-Host "`n== $message" -ForegroundColor Cyan }
function Write-Ok($message)   { Write-Host "   $message" -ForegroundColor Green }
function Write-Warn($message) { Write-Host "   $message" -ForegroundColor Yellow }

Write-Step "파이썬 확인"
$python = $null
foreach ($candidate in @("py -3", "python", "python3")) {
    $exe, $exeArgs = $candidate.Split(" ", 2)
    if (Get-Command $exe -ErrorAction SilentlyContinue) {
        $version = & $exe $exeArgs --version 2>&1
        if ($version -match "Python (\d+)\.(\d+)") {
            if ([int]$Matches[1] -eq 3 -and [int]$Matches[2] -ge 10) {
                $python = $candidate
                Write-Ok "$version ($exe)"
                break
            }
        }
    }
}

if (-not $python) {
    Write-Host @"

파이썬 3.10 이상이 필요합니다.
  https://www.python.org/downloads/ 에서 설치하세요.
  설치 화면에서 'Add Python to PATH' 를 꼭 체크해야 합니다.
"@ -ForegroundColor Red
    exit 1
}

$exe, $exeArgs = $python.Split(" ", 2)

Write-Step "가상환경 만들기 (.venv)"
if (-not (Test-Path ".venv")) {
    & $exe $exeArgs -m venv .venv
    Write-Ok "새로 만들었습니다"
} else {
    Write-Ok "이미 있어서 그대로 씁니다"
}

$venvPython = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $venvPython)) {
    Write-Host "가상환경 생성에 실패했습니다." -ForegroundColor Red
    exit 1
}

Write-Step "precut 설치 (몇 분 걸립니다)"
& $venvPython -m pip install --quiet --upgrade pip
& $venvPython -m pip install --quiet -e ".[all]"
Write-Ok "설치 완료"

Write-Step "ffmpeg 확인"
if (Get-Command ffmpeg -ErrorAction SilentlyContinue) {
    Write-Ok "시스템에 설치된 ffmpeg 사용"
} else {
    Write-Ok "동봉된 ffmpeg(imageio-ffmpeg) 사용"
}

Write-Step "음성 인식 모델"
Write-Warn "자막을 처음 만들 때 Whisper 모델을 자동으로 내려받습니다 (medium 기준 약 1.5GB)."
Write-Warn "GPU가 없으면 시간이 꽤 걸리니, 급하면 --model small 을 쓰세요."

$precut = Join-Path $root ".venv\Scripts\precut.exe"
Write-Host @"

설치가 끝났습니다.

실행 방법 (PowerShell):

  # 영상 하나
  & "$precut" "C:\경로\영상.mp4"

  # 폴더 통째로
  & "$precut" "C:\경로\영상폴더"

  # 자막 없이 컷과 소리 밸런스만 (빠름)
  & "$precut" "C:\경로\영상.mp4" --no-subtitles

  # 얼마나 잘릴지 먼저 확인
  & "$precut" "C:\경로\영상.mp4" --no-subtitles --dry-run

결과물은 영상과 같은 위치의 <파일명>_precut 폴더에 생깁니다.
프리미어에서 파일 > 가져오기 로 .xml 을 열면 컷이 적용된 시퀀스가 나옵니다.

"@ -ForegroundColor White
