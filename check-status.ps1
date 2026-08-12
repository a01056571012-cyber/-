# precut 상태 확인
#
# 이 파일을 직접 실행하지 말고, 같은 폴더의 "3-상태확인하기.bat" 을 더블클릭하세요.
# 어디까지 진행됐는지, 무엇이 빠졌는지 알려줍니다.

$ErrorActionPreference = "Continue"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path

function Write-Item($label, $ok, $detail) {
    $mark = if ($ok) { "[O]" } else { "[X]" }
    $color = if ($ok) { "Green" } else { "Red" }
    Write-Host ("{0} {1,-16} {2}" -f $mark, $label, $detail) -ForegroundColor $color
}

Write-Host "precut 상태 확인" -ForegroundColor White
Write-Host "설치 폴더: $root" -ForegroundColor DarkGray
Write-Host ""

$hasProject = Test-Path (Join-Path $root "pyproject.toml")
Write-Item "설치 폴더" $hasProject $(if ($hasProject) { "정상" } else { "pyproject.toml 이 없습니다. 폴더가 잘못됐습니다" })

$pythonFound = $false
$pythonDetail = "파이썬 3.10 이상을 찾지 못했습니다"
foreach ($candidate in @(@{ Exe = "py"; Extra = @("-3") }, @{ Exe = "python"; Extra = @() })) {
    if (-not (Get-Command $candidate.Exe -ErrorAction SilentlyContinue)) { continue }
    $version = ""
    try { $version = (& $candidate.Exe ($candidate.Extra + @("--version")) 2>&1) -join " " } catch { continue }
    if ($version -match "Python (\d+)\.(\d+)" -and [int]$Matches[1] -eq 3 -and [int]$Matches[2] -ge 10) {
        $pythonFound = $true
        $pythonDetail = $version
        break
    }
}
Write-Item "파이썬" $pythonFound $pythonDetail

$venv = Test-Path (Join-Path $root ".venv\Scripts\python.exe")
Write-Item "가상환경" $venv $(if ($venv) { ".venv 폴더 있음" } else { "아직 없음. 1-설치하기 를 실행하세요" })

$precut = Join-Path $root ".venv\Scripts\precut.exe"
$installed = Test-Path $precut
$precutDetail = "아직 설치되지 않음. 1-설치하기 를 실행하세요"
$canMerge = $false
if ($installed) {
    $version = ""
    try { $version = (& $precut "--version" 2>&1) -join " " } catch { $version = "실행 실패" }
    $precutDetail = $version
    try { $canMerge = ((& $precut "--help" 2>&1) -join " ") -match "--merge" } catch { $canMerge = $false }
}
Write-Item "precut" $installed $precutDetail
if ($installed) {
    Write-Item "이어붙이기" $canMerge $(if ($canMerge) {
        "여러 영상을 한 시퀀스로 합칠 수 있습니다"
    } else {
        "옛 버전입니다. 새 ZIP을 받아 1-설치하기 를 다시 실행하세요"
    })
}

Write-Host ""
$logPath = Join-Path $root "precut-log.txt"
if (Test-Path $logPath) {
    Write-Host "최근 기록 (precut-log.txt 마지막 25줄):" -ForegroundColor Cyan
    Write-Host "----------------------------------------------------------" -ForegroundColor DarkGray
    Get-Content $logPath -Tail 25
    Write-Host "----------------------------------------------------------" -ForegroundColor DarkGray
    Write-Host "문제가 있으면 이 내용을 복사해서 물어보세요." -ForegroundColor DarkGray
} else {
    Write-Host "아직 실행 기록(precut-log.txt)이 없습니다." -ForegroundColor DarkGray
}

if ($installed) {
    Write-Host ""
    Write-Host "영상을 처리하려면 2-영상처리하기 를 실행하세요." -ForegroundColor White
    Write-Host "결과물은 원본 영상 옆의 '<파일이름>_precut' 폴더에 생깁니다." -ForegroundColor White
}

Write-Host ""
Write-Host "아무 키나 누르면 창이 닫힙니다." -ForegroundColor DarkGray
