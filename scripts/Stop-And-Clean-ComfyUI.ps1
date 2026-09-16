<#
.SYNOPSIS
    Stops the ComfyUI Desktop app / service and (optionally) deletes the ComfyUI
    folders on C: and P:.

.DESCRIPTION
    SAFE BY DEFAULT. Running this with no switches performs a DRY RUN: it stops
    ComfyUI, then lists every folder it *would* delete (with sizes and whether the
    path is a real directory or a junction/symlink). Nothing is deleted until you
    re-run with -Execute and type DELETE at the confirmation prompt.

    Junction / symlink handling: if a folder on C: is actually a junction pointing
    into P: (or vice versa), the script removes the *link* without recursing into
    its target, then deletes the real target separately. This prevents the classic
    "deleted C: junction and it wiped P: too" accident.

.PARAMETER Paths
    One or more ComfyUI folders to remove. If omitted, the script auto-detects
    common ComfyUI Desktop / portable locations on C: and P:.

.PARAMETER Execute
    Actually delete. Without this switch the script only reports (dry run).

.PARAMETER Force
    Skip the interactive "type DELETE" confirmation. Use with -Execute for
    unattended runs. Dangerous — only use once you've reviewed a dry run.

.EXAMPLE
    # 1) See what would happen (recommended first step)
    .\Stop-And-Clean-ComfyUI.ps1

.EXAMPLE
    # 2) Delete the auto-detected folders, with a confirmation prompt
    .\Stop-And-Clean-ComfyUI.ps1 -Execute

.EXAMPLE
    # 3) Delete specific folders you name yourself
    .\Stop-And-Clean-ComfyUI.ps1 -Paths 'C:\ComfyUI','P:\ComfyUI' -Execute
#>

[CmdletBinding()]
param(
    [string[]] $Paths,
    [switch]   $Execute,
    [switch]   $Force
)

$ErrorActionPreference = 'Stop'

function Write-Section($text) {
    Write-Host ""
    Write-Host "==== $text ====" -ForegroundColor Cyan
}

# ---------------------------------------------------------------------------
# 1. Stop ComfyUI Desktop app / service / any process on the ComfyUI port
# ---------------------------------------------------------------------------
function Stop-ComfyUI {
    Write-Section "Stopping ComfyUI"

    # a) Windows service named like ComfyUI (if one was installed)
    Get-Service -ErrorAction SilentlyContinue |
        Where-Object { $_.Name -match 'comfy' -or $_.DisplayName -match 'comfy' } |
        ForEach-Object {
            Write-Host "Stopping service: $($_.DisplayName) [$($_.Status)]" -ForegroundColor Yellow
            try { Stop-Service -Name $_.Name -Force -ErrorAction Stop }
            catch { Write-Warning "  Could not stop service $($_.Name): $($_.Exception.Message)" }
        }

    # b) Desktop app / electron / python processes belonging to ComfyUI
    $procNames = @('ComfyUI', 'comfyui', 'comfyui-electron', '@comfyorgcomfyui-electron')
    $procs = Get-Process -ErrorAction SilentlyContinue | Where-Object {
        $procNames -contains $_.ProcessName -or
        ($_.Path -and $_.Path -match 'comfy') -or
        ($_.MainWindowTitle -and $_.MainWindowTitle -match 'ComfyUI')
    }
    foreach ($p in $procs) {
        Write-Host "Stopping process: $($p.ProcessName) (PID $($p.Id))" -ForegroundColor Yellow
        try { Stop-Process -Id $p.Id -Force -ErrorAction Stop }
        catch { Write-Warning "  Could not stop PID $($p.Id): $($_.Exception.Message)" }
    }

    # c) Anything still listening on the default ComfyUI port (8188)
    try {
        $conns = Get-NetTCPConnection -State Listen -LocalPort 8188 -ErrorAction SilentlyContinue
        foreach ($c in ($conns.OwningProcess | Sort-Object -Unique)) {
            $p = Get-Process -Id $c -ErrorAction SilentlyContinue
            if ($p) {
                Write-Host "Stopping process on port 8188: $($p.ProcessName) (PID $($p.Id))" -ForegroundColor Yellow
                try { Stop-Process -Id $p.Id -Force -ErrorAction Stop }
                catch { Write-Warning "  Could not stop PID $($p.Id): $($_.Exception.Message)" }
            }
        }
    } catch { }

    Start-Sleep -Seconds 2
    Write-Host "ComfyUI stop routine complete." -ForegroundColor Green
}

# ---------------------------------------------------------------------------
# 2. Discover candidate ComfyUI folders (only used if -Paths not supplied)
# ---------------------------------------------------------------------------
function Get-CandidatePaths {
    $local = $env:LOCALAPPDATA
    $roaming = $env:APPDATA
    $userProfile = $env:USERPROFILE

    $candidates = @(
        # ComfyUI Desktop app install + data
        (Join-Path $local  'Programs\@comfyorgcomfyui-electron'),
        (Join-Path $local  'Programs\comfyui-electron'),
        (Join-Path $roaming 'ComfyUI'),
        # Portable / manual installs
        'C:\ComfyUI',
        'C:\ComfyUI_windows_portable',
        (Join-Path $userProfile 'ComfyUI'),
        (Join-Path $userProfile 'Documents\ComfyUI'),
        (Join-Path $userProfile 'Desktop\ComfyUI'),
        # P: drive (common for large model storage)
        'P:\ComfyUI',
        'P:\ComfyUI_windows_portable',
        'P:\comfyui'
    )

    $candidates | Where-Object { Test-Path -LiteralPath $_ } | Sort-Object -Unique
}

# ---------------------------------------------------------------------------
# 3. Classify a path: real directory vs reparse point (junction/symlink)
# ---------------------------------------------------------------------------
function Get-PathInfo($path) {
    $item = Get-Item -LiteralPath $path -Force
    $isLink = [bool]($item.Attributes -band [IO.FileAttributes]::ReparsePoint)
    $target = if ($isLink) { $item.Target } else { $null }

    $sizeGB = $null
    if (-not $isLink) {
        try {
            $bytes = (Get-ChildItem -LiteralPath $path -Recurse -Force -File -ErrorAction SilentlyContinue |
                      Measure-Object -Property Length -Sum).Sum
            if ($bytes) { $sizeGB = [math]::Round($bytes / 1GB, 2) }
        } catch { }
    }

    [pscustomobject]@{
        Path   = $item.FullName
        Type   = if ($isLink) { 'JUNCTION/SYMLINK' } else { 'REAL DIRECTORY' }
        Target = $target
        SizeGB = $sizeGB
    }
}

# ---------------------------------------------------------------------------
# 4. Delete: links first (no recurse into target), then real directories
# ---------------------------------------------------------------------------
function Remove-ComfyPath($info) {
    if ($info.Type -eq 'JUNCTION/SYMLINK') {
        # Remove only the reparse point; do NOT follow into the target.
        Write-Host "Removing link: $($info.Path)  ->  $($info.Target)" -ForegroundColor Yellow
        # .Delete() on a DirectoryInfo reparse point removes the link, not the target.
        (Get-Item -LiteralPath $info.Path -Force).Delete()
    } else {
        Write-Host "Deleting directory: $($info.Path)" -ForegroundColor Yellow
        Remove-Item -LiteralPath $info.Path -Recurse -Force
    }
}

# ===========================================================================
# MAIN
# ===========================================================================
Stop-ComfyUI

Write-Section "Resolving folders to delete"
if (-not $Paths -or $Paths.Count -eq 0) {
    $Paths = Get-CandidatePaths
    Write-Host "No -Paths given; auto-detected the following:" -ForegroundColor Cyan
}

if (-not $Paths -or $Paths.Count -eq 0) {
    Write-Host "No ComfyUI folders found. Pass them explicitly with -Paths if they live elsewhere." -ForegroundColor Green
    return
}

$targets = @()
foreach ($p in $Paths) {
    if (Test-Path -LiteralPath $p) {
        $targets += (Get-PathInfo $p)
    } else {
        Write-Warning "Not found (skipping): $p"
    }
}

if ($targets.Count -eq 0) { Write-Host "Nothing to delete." -ForegroundColor Green; return }

$targets | Format-Table Path, Type, SizeGB, Target -AutoSize

if (-not $Execute) {
    Write-Host ""
    Write-Host "DRY RUN — nothing was deleted. Re-run with -Execute to delete the folders above." -ForegroundColor Green
    return
}

# --- Actual deletion path ---
Write-Section "DELETE"
Write-Host "You are about to PERMANENTLY DELETE the folders listed above." -ForegroundColor Red
if (-not $Force) {
    $answer = Read-Host "Type DELETE (in capitals) to proceed"
    if ($answer -cne 'DELETE') {
        Write-Host "Aborted — no changes made." -ForegroundColor Green
        return
    }
}

# Delete links before real dirs so a link is never followed into a target
# we also intend to remove.
foreach ($info in ($targets | Sort-Object { $_.Type -ne 'JUNCTION/SYMLINK' })) {
    try {
        Remove-ComfyPath $info
        Write-Host "  Removed: $($info.Path)" -ForegroundColor Green
    } catch {
        Write-Warning "  FAILED to remove $($info.Path): $($_.Exception.Message)"
    }
}

Write-Section "Done"
Write-Host "ComfyUI stopped and requested folders removed." -ForegroundColor Green
