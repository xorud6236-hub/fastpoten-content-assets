# setup_data_link.ps1 — 데이터(창고 DB + 이미지 corpus)를 구글드라이브로 동기화 연결.
#
# 왜: 코드는 깃허브로 이어지지만 data/·corpus/는 (용량·개인정보 때문에) 깃 제외라 PC마다 없다.
#     이 스크립트가 두 폴더를 [구글드라이브]\fastpoten-data 에 두고, 프로젝트 자리엔 '바로가기(junction)'를
#     걸어 두 PC가 같은 데이터를 쓰게 한다. 개인정보는 내 개인 구글드라이브에만(공개 서버 아님).
#
# 사전: 구글드라이브 데스크톱 앱(Google Drive for Desktop) 설치·로그인 상태여야 함(G: 등으로 마운트).
# 사용(각 PC에서 1회):  프로젝트 폴더에서
#   powershell -ExecutionPolicy Bypass -File setup_data_link.ps1
#
# 규칙(중요): 한 번에 한 PC에서만 작업. PC 바꾸기 전 뷰어/추출을 끄고 구글드라이브 동기화가
#            끝난 뒤 다른 PC를 켠다(같은 DB를 동시에 쓰면 손상 위험).

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

function Get-GoogleDriveRoot {
    # 한글 폴더명('내 드라이브')에 의존하면 PowerShell 인코딩 문제가 있으므로,
    # 볼륨명 'Google Drive'로 드라이브를 찾고 그 안의 개인 드라이브 폴더를 고른다(언어 무관).
    $vol = Get-CimInstance Win32_LogicalDisk -ErrorAction SilentlyContinue |
           Where-Object { $_.VolumeName -eq "Google Drive" } | Select-Object -First 1
    if (-not $vol) { return $null }
    $root = $vol.DeviceID + "\"
    $subs = @(Get-ChildItem -LiteralPath $root -Directory -ErrorAction SilentlyContinue)
    if ($subs.Count -eq 0) { return $null }
    if ($subs.Count -eq 1) { return $subs[0].FullName }          # 개인 드라이브 하나뿐(내 드라이브/My Drive)
    $md = $subs | Where-Object { $_.Name -eq "My Drive" } | Select-Object -First 1
    if ($md) { return $md.FullName }
    $ns = $subs | Where-Object { $_.Name -ne "Shared drives" } | Select-Object -First 1
    if ($ns) { return $ns.FullName }
    return $subs[0].FullName
}

$gdRoot = Get-GoogleDriveRoot
if (-not $gdRoot) {
    Write-Host "구글드라이브를 찾지 못했습니다. Google Drive for Desktop을 설치·로그인한 뒤 다시 실행하세요." -ForegroundColor Red
    Write-Host "(보통 G: 드라이브의 '내 드라이브' 또는 'My Drive' 폴더로 마운트됩니다.)" -ForegroundColor Red
    exit 1
}
$dest = Join-Path $gdRoot "fastpoten-data"
New-Item -ItemType Directory -Force -Path $dest | Out-Null
Write-Host "동기화 폴더: $dest"

foreach ($name in @("data", "corpus")) {
    $target = Join-Path $dest $name
    $full = Join-Path $PSScriptRoot $name
    $cur = Get-Item -LiteralPath $full -Force -ErrorAction SilentlyContinue

    if ($cur -and $cur.LinkType) {
        Write-Host ("  {0}: 이미 연결됨 (건너뜀)" -f $name) -ForegroundColor Green
        continue
    }
    if ($cur -and -not $cur.LinkType) {
        # 이 PC에 실제 데이터가 있음 → 구글드라이브로 이동(대상이 비어 있을 때만 자동)
        if (Test-Path $target) {
            Write-Host ("  {0}: 구글드라이브와 로컬 양쪽에 있습니다. 자동 이동 안 함 — 어느 쪽을 쓸지 직접 확인하세요." -f $name) -ForegroundColor Yellow
            continue
        }
        Move-Item -LiteralPath $full -Destination $target
        Write-Host ("  {0}: 구글드라이브로 이동" -f $name)
    }
    New-Item -ItemType Directory -Force -Path $target | Out-Null
    if (-not (Test-Path $full)) {
        New-Item -ItemType Junction -Path $full -Target $target | Out-Null
        Write-Host ("  {0}: 연결(junction) 생성 -> {1}" -f $name, $target)
    }
}

Write-Host ""
Write-Host "완료. 구글드라이브 동기화가 끝나면 'python src/viewer.py' 로 켜세요." -ForegroundColor Green
Write-Host "팁: 구글드라이브에서 'fastpoten-data' 폴더 우클릭 → '오프라인 사용 가능'으로 두면 오프라인·안정적입니다." -ForegroundColor Cyan
