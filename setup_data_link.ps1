# setup_data_link.ps1 — 데이터(창고 DB + 이미지 corpus)를 OneDrive로 동기화 연결.
#
# 왜: 코드는 깃허브로 이어지지만 data/·corpus/는 (용량·개인정보 때문에) 깃 제외라 PC마다 없다.
#     이 스크립트가 두 폴더를 OneDrive\fastpoten-data 에 두고, 프로젝트 자리엔 '바로가기(junction)'를
#     걸어 두 PC가 같은 데이터를 쓰게 한다. 개인정보는 내 개인 OneDrive에만(공개 서버 아님).
#
# 사용(각 PC에서 1회):  프로젝트 폴더에서
#   powershell -ExecutionPolicy Bypass -File setup_data_link.ps1
#
# 규칙(중요): 한 번에 한 PC에서만 작업. PC 바꾸기 전 뷰어/추출을 끄고 OneDrive 동기화(초록 체크)가
#            끝난 뒤 다른 PC를 켠다(같은 DB를 동시에 쓰면 손상 위험).

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

$one = $env:OneDrive
if (-not $one) {
    Write-Host "OneDrive를 찾지 못했습니다. OneDrive에 로그인한 뒤 다시 실행하세요." -ForegroundColor Red
    exit 1
}
$dest = Join-Path $one "fastpoten-data"
New-Item -ItemType Directory -Force -Path $dest | Out-Null
Write-Host "동기화 폴더: $dest"

foreach ($name in @("data", "corpus")) {
    $target = Join-Path $dest $name
    $cur = Get-Item $name -ErrorAction SilentlyContinue

    if ($cur -and $cur.LinkType) {
        Write-Host ("  {0}: 이미 연결됨 (건너뜀)" -f $name) -ForegroundColor Green
        continue
    }
    if ($cur -and -not $cur.LinkType) {
        # 이 PC에 실제 데이터가 있음 → OneDrive로 이동(대상이 비어 있을 때만 자동)
        if (Test-Path $target) {
            Write-Host ("  {0}: OneDrive와 로컬 양쪽에 있습니다. 자동 이동 안 함 — 어느 쪽을 쓸지 직접 확인하세요." -f $name) -ForegroundColor Yellow
            continue
        }
        Move-Item $name $target
        Write-Host ("  {0}: OneDrive로 이동" -f $name)
    }
    # 대상 폴더 보장(아직 동기화 안 됐어도 유효한 연결이 되도록)
    New-Item -ItemType Directory -Force -Path $target | Out-Null
    if (-not (Test-Path $name)) {
        New-Item -ItemType Junction -Path $name -Target $target | Out-Null
        Write-Host ("  {0}: 연결(junction) 생성 -> {1}" -f $name, $target)
    }
}

Write-Host ""
Write-Host "완료. OneDrive 동기화가 끝나면(초록 체크) 'python src/viewer.py' 로 켜세요." -ForegroundColor Green
Write-Host "팁: OneDrive에서 'fastpoten-data' 폴더를 우클릭 → '이 장치에 항상 유지'로 두면 오프라인에서도 됩니다." -ForegroundColor Cyan
