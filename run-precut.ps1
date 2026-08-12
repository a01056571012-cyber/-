# precut 실행 도우미
#
# 이 파일을 직접 실행하지 말고, 같은 폴더의 "2-영상처리하기.bat" 을 더블클릭하세요.

param([string]$Path = "")

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path

$logPath = Join-Path $root "precut-log.txt"
try { Start-Transcript -Path $logPath -Append | Out-Null } catch { }


function Write-Step($message) { Write-Host "`n== $message" -ForegroundColor Cyan }
function Write-Ok($message)   { Write-Host "   $message" -ForegroundColor Green }

function Invoke-Precut {
    Set-Location $root
    $precut = Join-Path $root ".venv\Scripts\precut.exe"
    if (-not (Test-Path $precut)) {
        throw "precut이 아직 설치되지 않았습니다. 먼저 1-설치하기.bat 을 실행하세요."
    }

    Write-Host "precut 실행" -ForegroundColor White

    $target = $Path
    if (-not $target) {
        Write-Host ""
        Write-Host "처리할 영상 파일이나 폴더의 경로를 입력하세요." -ForegroundColor DarkGray
        Write-Host "탐색기에서 파일을 이 창으로 끌어다 놓아도 됩니다." -ForegroundColor DarkGray
        Write-Host ""
        $target = Read-Host "경로"
    }

    $target = $target.Trim().Trim('"')
    if (-not $target) { throw "경로가 비어 있습니다." }
    if (-not (Test-Path $target)) {
        throw "그런 파일이나 폴더가 없습니다: $target"
    }

    Write-Step "프리셋 고르기"
    Write-Host "   1. talking-head  인터뷰, 강좌 등 말하는 영상 (기본)"
    Write-Host "   2. vlog          현장음이 있는 브이로그"
    Write-Host "   3. lecture       긴 강의. 호흡을 더 남김"
    Write-Host "   4. tight         최대한 짧게. 숏폼용"
    Write-Host "   5. gentle        확실한 무음만 제거"
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
    & $precut $target "--preset" $preset "--no-subtitles" "--dry-run"
    if ($LASTEXITCODE -ne 0) { throw "분석에 실패했습니다. 위 메시지를 확인하세요." }

    Write-Host ""
    $answer = Read-Host "이대로 편집 파일을 만들까요? (Y/N)"
    if ($answer -notmatch "^[Yy]") {
        Write-Host "취소했습니다. 컷이 과하면 gentle, 부족하면 tight 프리셋으로 다시 시도해 보세요." -ForegroundColor Yellow
        return
    }

    $subtitleAnswer = Read-Host "자막도 만들까요? (Y/N, 처음 한 번은 인식 모델 약 1.5GB를 내려받습니다)"
    $options = @("--preset", $preset)
    if ($subtitleAnswer -notmatch "^[Yy]") { $options += "--no-subtitles" }

    Write-Step "처리 중 (영상 길이에 따라 시간이 걸립니다)"
    & $precut $target $options
    if ($LASTEXITCODE -ne 0) { throw "처리 중 오류가 발생했습니다." }

    if (Test-Path $target -PathType Container) {
        $resultFolder = $target
    } else {
        $item = Get-Item $target
        $resultFolder = Join-Path $item.DirectoryName ($item.BaseName + "_precut")
    }

    Write-Host ""
    Write-Host "끝났습니다." -ForegroundColor Green
    Write-Host "결과물은 원본 영상 옆의 '<파일이름>_precut' 폴더에 있습니다." -ForegroundColor White
    Write-Host "프리미어에서 파일 > 가져오기 로 그 안의 .xml 을 열면 컷이 적용된 시퀀스가 나옵니다." -ForegroundColor White
    Write-Host ""

    if (Test-Path $resultFolder) {
        $open = Read-Host "결과 폴더를 열까요? (Y/N)"
        if ($open -match "^[Yy]") { Start-Process explorer.exe $resultFolder }
    }
}

try {
    Invoke-Precut
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
