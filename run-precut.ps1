# precut 실행 도우미
#
# 사용법: 이 파일을 탐색기에서 우클릭 → "PowerShell에서 실행"
#         영상 파일이나 폴더 경로를 물어보면 붙여넣으면 됩니다.
#         (탐색기에서 파일을 이 창으로 끌어다 놓아도 경로가 입력됩니다)

param([string]$Path = "")

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

function Write-Step($message) { Write-Host "`n== $message" -ForegroundColor Cyan }
function Write-Ok($message)   { Write-Host "   $message" -ForegroundColor Green }
function Write-Bad($message)  { Write-Host "   $message" -ForegroundColor Red }

function Pause-Exit($code) {
    Write-Host "`n계속하려면 아무 키나 누르세요..." -ForegroundColor DarkGray
    try { $null = $Host.UI.RawUI.ReadKey("NoEcho,IncludeKeyDown") } catch { Read-Host }
    exit $code
}

$precut = Join-Path $root ".venv\Scripts\precut.exe"
if (-not (Test-Path $precut)) {
    Write-Bad "precut이 아직 설치되지 않았습니다."
    Write-Host "   같은 폴더의 setup-windows.ps1 을 먼저 실행하세요." -ForegroundColor Yellow
    Pause-Exit 1
}

Write-Host "precut 실행" -ForegroundColor White

if (-not $Path) {
    Write-Host @"

처리할 영상 파일이나 폴더의 경로를 입력하세요.
탐색기에서 파일을 이 창으로 끌어다 놓아도 됩니다.

"@ -ForegroundColor DarkGray
    $Path = Read-Host "경로"
}

$Path = $Path.Trim().Trim('"')
if (-not $Path) {
    Write-Bad "경로가 비어 있습니다."
    Pause-Exit 1
}
if (-not (Test-Path $Path)) {
    Write-Bad "그런 파일이나 폴더가 없습니다: $Path"
    Write-Host "   경로에 오타가 없는지, 따옴표가 섞이지 않았는지 확인하세요." -ForegroundColor Yellow
    Pause-Exit 1
}

Write-Step "프리셋 고르기"
Write-Host @"
   1. talking-head  인터뷰·강좌 등 말하는 영상 (기본)
   2. vlog          현장음이 있는 브이로그
   3. lecture       긴 강의. 호흡을 더 남김
   4. tight         최대한 짧게. 숏폼용
   5. gentle        확실한 무음만 제거
"@
$presetChoice = Read-Host "번호 (그냥 Enter 치면 1)"
$preset = switch ($presetChoice) {
    "2" { "vlog" }
    "3" { "lecture" }
    "4" { "tight" }
    "5" { "gentle" }
    default { "talking-head" }
}
Write-Ok $preset

Write-Step "얼마나 잘리는지 먼저 확인합니다 (파일은 만들지 않음)"
& $precut $Path "--preset" $preset "--no-subtitles" "--dry-run"
if ($LASTEXITCODE -ne 0) {
    Write-Bad "분석에 실패했습니다. 위 메시지를 확인하세요."
    Pause-Exit 1
}

Write-Host ""
$answer = Read-Host "이대로 편집 파일을 만들까요? (Y/N)"
if ($answer -notmatch "^[Yy]") {
    Write-Host "취소했습니다. 컷이 과하면 -p gentle, 부족하면 -p tight 로 다시 시도해 보세요." -ForegroundColor Yellow
    Pause-Exit 0
}

$subtitleAnswer = Read-Host "자막도 만들까요? (Y/N, 처음 한 번은 인식 모델 약 1.5GB를 내려받습니다)"
$options = @("--preset", $preset)
if ($subtitleAnswer -notmatch "^[Yy]") {
    $options += "--no-subtitles"
}

Write-Step "처리 중 (영상 길이에 따라 시간이 걸립니다)"
& $precut $Path $options
if ($LASTEXITCODE -ne 0) {
    Write-Bad "처리 중 오류가 발생했습니다."
    Pause-Exit 1
}

$resultFolder = $null
if (Test-Path $Path -PathType Container) {
    $resultFolder = $Path
} else {
    $item = Get-Item $Path
    $resultFolder = Join-Path $item.DirectoryName ($item.BaseName + "_precut")
}

Write-Host @"

끝났습니다.

결과물은 원본 영상 옆의 '<파일이름>_precut' 폴더에 있습니다.
프리미어에서 파일 > 가져오기 로 그 안의 .xml 을 열면 컷이 적용된 시퀀스가 나옵니다.

"@ -ForegroundColor White

if ($resultFolder -and (Test-Path $resultFolder)) {
    $open = Read-Host "결과 폴더를 열까요? (Y/N)"
    if ($open -match "^[Yy]") { Start-Process explorer.exe $resultFolder }
}

Pause-Exit 0
