# Undo install-winusb.ps1: remove the recorder WinUSB driver package from this PC (the
# recorders go back to "no driver") and every certificate the setup created.
# Every package OpenEVP ever installed (v0.5.0 on, the ST25-only ones included) is called
# st25_winusb.inf whichever models it covers, so all of them are found by that name; this
# needs no driver manifest and works even if models.json is gone.
# Runs elevated (the uninstaller). Same hardening as install-winusb.ps1: modules only
# from Windows' own folder, native tools by full path. Each step runs on its own, so
# a failure in one never skips the others. Logged to <install folder>\driver-setup\
# uninstall.log, which the uninstaller shows if something could not be removed.
#
# Exit code: 0 removed, 3010 removed but Windows wants a restart, 1 something is left.

$ErrorActionPreference = "Continue"
$sys = [Environment]::GetFolderPath("System")
$env:PSModulePath = Join-Path $sys "WindowsPowerShell\v1.0\Modules"
$pnputil = Join-Path $sys "pnputil.exe"

$logDir = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot "..\..\driver-setup"))
New-Item -ItemType Directory -Force $logDir | Out-Null
$log = Join-Path $logDir "uninstall.log"
function Log($msg) {
    try { Add-Content -Path $log -Value ("{0:HH:mm:ss} {1}" -f (Get-Date), $msg) } catch { }
}
Set-Content -Path $log -Value "OpenEVP WinUSB removal"

$failed = $false
$restart = $false
try {
    Import-Module (Join-Path $env:PSModulePath "Dism") -ErrorAction Stop
    foreach ($d in @(Get-WindowsDriver -Online -ErrorAction Stop | Where-Object { $_.OriginalFileName -like "*\st25_winusb.inf" })) {
        $out = & $pnputil /delete-driver $d.Driver /uninstall /force 2>&1 | Out-String
        Log "delete $($d.Driver): exit $LASTEXITCODE`n$out"
        if ($LASTEXITCODE -eq 3010) { $restart = $true }
        elseif ($LASTEXITCODE -ne 0) { $failed = $true; Log "FAILED: could not remove driver package $($d.Driver)" }
    }
} catch {
    $failed = $true
    Log "FAILED: could not list driver packages: $($_.Exception.Message)"
}

# Independent of the step above. Matches the current and the earlier name
# ("ST25 Downloader"), including early names with a comma, stored quoted (CN="...").
foreach ($store in "Root", "TrustedPublisher", "My") {
    try {
        foreach ($c in @(Get-ChildItem "Cert:\LocalMachine\$store" -ErrorAction Stop |
                         Where-Object { $_.Subject -like "*OpenEVP driver signer*" -or
                                        $_.Subject -like "*ST25 Downloader driver signer*" })) {
            try { Remove-Item -Path $c.PSPath -DeleteKey -ErrorAction Stop; Log "removed certificate $($c.Thumbprint) from $store" }
            catch { $failed = $true; Log "FAILED: could not remove certificate $($c.Thumbprint) from ${store}: $($_.Exception.Message)" }
        }
    } catch {
        $failed = $true
        Log "FAILED: could not read the $store certificate store: $($_.Exception.Message)"
    }
}
Log $(if ($failed) { "DONE with errors" } elseif ($restart) { "DONE (restart needed)" } else { "DONE" })
exit $(if ($failed) { 1 } elseif ($restart) { 3010 } else { 0 })
