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

    $mergeOption = @()
    if (Test-Path $target -PathType Container) {
        Write-Step "여러 영상을 어떻게 처리할까요"
        Write-Host "   1. 하나의 시퀀스로 이어붙이기 (기본)"
        Write-Host "      - 영상들이 순서대로 이어진 타임라인 하나가 만들어집니다"
        Write-Host "      - 영상들 사이의 음량 차이까지 함께 맞춥니다"
        Write-Host "   2. 각각 따로 편집하기"
        Write-Host "      - 영상마다 시퀀스가 하나씩 따로 만들어집니다"
        $mergeChoice = Read-Host "번호 (그냥 Enter 치면 1)"
        if ($mergeChoice -ne "2") {
            $mergeOption = @("--merge")
            Write-Ok "하나로 이어붙입니다"
        } else {
            Write-Ok "각각 따로 편집합니다"
        }
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

    $extra = @()

    Write-Step "맨 앞을 지킬까요"
    Write-Host "   영상 맨 앞 인사말이 잘려나가지 않도록 통째로 남길 수 있습니다."
    $headAnswer = (Read-Host "맨 앞 몇 초를 남길까요? (그냥 Enter 치면 20초, 0이면 안 남김)").Trim()
    if (-not $headAnswer) { $headAnswer = "20" }
    if ($headAnswer -match "^\d+$" -and [int]$headAnswer -gt 0) {
        $extra += @("--keep-head", "$($headAnswer)s")
        Write-Ok "앞 $($headAnswer)초를 지킵니다"
    }

    Write-Step "자르는 기준"
    Write-Host "   1. 소리 기준 (빠름, 기본)"
    Write-Host "      - 소리가 작은 곳을 잘라냅니다. 말 중간에서 잘릴 수 있습니다."
    Write-Host "   2. 문장 기준 (맥락 유지)"
    Write-Host "      - 말이 끝난 자리에서만 자릅니다. 음성 인식이 필요해 오래 걸립니다."
    $cutChoice = Read-Host "번호 (그냥 Enter 치면 1)"
    $bySentence = $cutChoice -eq "2"
    if ($bySentence) {
        $extra += @("--cut-by", "sentence")
        Write-Ok "문장 기준"
    }

    Write-Step "목표 길이"
    Write-Host "   원하는 길이를 정하면 거기에 맞춰 더 잘라냅니다. (예: 30m, 1h20m)"
    $targetAnswer = (Read-Host "목표 길이 (그냥 Enter 치면 무음만 제거)").Trim()
    if ($targetAnswer) {
        $extra += @("-t", $targetAnswer)
        Write-Ok $targetAnswer
    }

    if (-not $bySentence) {
        Write-Step "얼마나 잘리는지 먼저 확인합니다 (파일은 만들지 않음)"
        & $precut $target ($mergeOption + $extra + @("--preset", $preset, "--no-subtitles", "--dry-run"))
        if ($LASTEXITCODE -ne 0) { throw "분석에 실패했습니다. 위 메시지를 확인하세요." }

        Write-Host ""
        $answer = Read-Host "이대로 편집 파일을 만들까요? (Y/N)"
        if ($answer -notmatch "^[Yy]") {
            Write-Host "취소했습니다. 컷이 과하면 gentle, 부족하면 tight 프리셋으로 다시 시도해 보세요." -ForegroundColor Yellow
            return
        }
    }

    $subtitleAnswer = Read-Host "자막도 만들까요? (Y/N)"
    $options = $mergeOption + $extra + @("--preset", $preset)
    if ($subtitleAnswer -match "^[Yy]") {
        Write-Step "자막 정확도 고르기"
        Write-Host "   1. 빠름   (small)     10분 영상에 약 3~8분, 모델 0.5GB (기본)"
        Write-Host "   2. 보통   (medium)    10분 영상에 약 8~20분, 모델 1.5GB"
        Write-Host "   3. 정확   (large-v3)  10분 영상에 30분 이상, 모델 3GB"
        Write-Host "   그래픽카드(NVIDIA)가 없으면 1번을 권합니다."
        $modelChoice = Read-Host "번호 (그냥 Enter 치면 1)"
        $model = switch ($modelChoice) {
            "2" { "medium" }
            "3" { "large-v3" }
            default { "small" }
        }
        $options += @("--model", $model)
        Write-Ok $model
        Write-Host ""
        Write-Host "음성 인식은 이 프로그램에서 가장 오래 걸리는 단계입니다." -ForegroundColor Yellow
        Write-Host "처음 한 번은 인식 모델을 내려받느라 몇 분 더 걸립니다." -ForegroundColor Yellow
        Write-Host "진행률이 표시되니 창을 닫지 말고 기다려 주세요." -ForegroundColor Yellow
    } else {
        $options += "--no-subtitles"
    }

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
    Write-Host "그 안의 '_대본.txt' 를 열어 필요 없는 줄을 지우고 다시 넣으면" -ForegroundColor White
    Write-Host "고른 구간만으로 편집본을 다시 만들 수 있습니다." -ForegroundColor White
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
