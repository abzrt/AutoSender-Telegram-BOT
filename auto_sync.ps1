<#
.SYNOPSIS
    Auto-Sync ke GitHub (dan otomatis deploy ke Railway) setiap kali kode berubah.
.DESCRIPTION
    Memantau perubahan file proyek via `git status --porcelain`.
    Setelah jeda debounce (default 5 detik), script menjalankan `pytest`,
    lalu otomatis `git add -A`, `git commit`, dan `git push origin main`.
#>

param(
    [int]$PollSeconds = 3,
    [int]$DebounceSeconds = 5,
    [switch]$Once
)

$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot

$PythonExe = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
$LastSnapshot = ""
$LastChangeAt = $null

Write-Host "====================================================" -ForegroundColor Cyan
Write-Host " Auto-Sync GitHub -> Railway AKTIF" -ForegroundColor Cyan
Write-Host " Folder : $PSScriptRoot" -ForegroundColor Gray
Write-Host " Tekan Ctrl+C untuk berhenti." -ForegroundColor Gray
Write-Host "====================================================" -ForegroundColor Cyan

function Invoke-SyncIfClean {
    Write-Host "`n[$(Get-Date -Format 'HH:mm:ss')] Perubahan terdeteksi! Menjalankan unit test..." -ForegroundColor Yellow

    if (Test-Path $PythonExe) {
        & $PythonExe -m pytest -q
        if ($LASTEXITCODE -ne 0) {
            Write-Host "[$(Get-Date -Format 'HH:mm:ss')] GAGAL: Unit test error. Auto-push dibatalkan sampai kode diperbaiki." -ForegroundColor Red
            return
        }
    }

    git add -A
    $staged = (git diff --cached --name-only) -join ", "
    if (-not $staged) {
        return
    }

    $msg = "Auto-update ($(Get-Date -Format 'yyyy-MM-dd HH:mm:ss'))"
    Write-Host "[$(Get-Date -Format 'HH:mm:ss')] Commit & push ($staged)..." -ForegroundColor Cyan
    git commit -m $msg | Out-Null
    git push origin main

    if ($LASTEXITCODE -eq 0) {
        Write-Host "[$(Get-Date -Format 'HH:mm:ss')] SUKSES di-push ke GitHub!" -ForegroundColor Green
        if (Get-Command railway.cmd -ErrorAction SilentlyContinue) {
            Write-Host "[$(Get-Date -Format 'HH:mm:ss')] Memicu deploy terbaru di Railway..." -ForegroundColor Cyan
            railway.cmd redeploy --from-source --yes | Out-Null
            Write-Host "[$(Get-Date -Format 'HH:mm:ss')] Deploy Railway dipicu!" -ForegroundColor Green
        }
    } else {
        Write-Host "[$(Get-Date -Format 'HH:mm:ss')] GAGAL push ke GitHub (cek koneksi/auth Git)." -ForegroundColor Red
    }
}

if ($Once) {
    $status = (git status --porcelain) -join "`n"
    if ($status) {
        Invoke-SyncIfClean
    } else {
        Write-Host "[$(Get-Date -Format 'HH:mm:ss')] Tidak ada perubahan untuk di-push." -ForegroundColor Green
    }
    exit 0
}

while ($true) {
    $status = (git status --porcelain) -join "`n"

    if ($status) {
        if ($status -ne $LastSnapshot) {
            $LastSnapshot = $status
            $LastChangeAt = Get-Date
            Write-Host "[$(Get-Date -Format 'HH:mm:ss')] File berubah, menunggu $DebounceSeconds detik sebelum sync..." -ForegroundColor DarkYellow
        } elseif ($LastChangeAt -and ((Get-Date) - $LastChangeAt).TotalSeconds -ge $DebounceSeconds) {
            Invoke-SyncIfClean
            $LastSnapshot = (git status --porcelain) -join "`n"
            $LastChangeAt = $null
        }
    } else {
        $LastSnapshot = ""
        $LastChangeAt = $null
    }

    Start-Sleep -Seconds $PollSeconds
}
