# Bind the recorders that need it (today USB 054C:0103: the Sony ICD-ST25, and the
# ICD-ST10, which shares its USB id) to Windows' built-in WinUSB driver.
#
# Which recorders: models.json next to this script, the driver manifest written at build
# time from the recorder registry (tools/make_driver_manifest.py): the models, their USB
# hardware ids, the driver version and the INF text. Read and checked by manifest.ps1.
#
# Runs elevated (the installer, or the app's "Set up recorder" button). Uses only
# tools built into Windows:
#   1. write the manifest's INF, which includes Microsoft's signed winusb.inf for
#      those devices
#   2. catalog it (New-FileCatalog) and sign the catalog with a certificate made
#      here and now (New-SelfSignedCertificate)
#   3. trust that certificate on this PC (Root + TrustedPublisher), then delete
#      its private key right after signing
#   4. install the driver package (pnputil), then check every connected recorder
#      really uses WinUSB
# If our package of this version is already in the driver store it is installed
# again onto the recorder; other versions are removed only after this one is in
# place. Works with or without the recorder plugged in: Windows applies the
# driver when it is plugged in (any USB port).
#
# Hardening (this runs with admin rights):
#   - modules load only from Windows' own folder, never from the user's module
#     path; native tools are called by full System32 path
#   - everything is written to admin-only folders: the log to
#     <install folder>\driver-setup, the package to a fresh folder only
#     Administrators and SYSTEM can access; nothing is taken from the caller
#
# Exit code: 0 done, 3010 done but Windows wants a restart, 1 failed (see setup.log).

$ErrorActionPreference = "Stop"
$sys = [Environment]::GetFolderPath("System")
$env:PSModulePath = Join-Path $sys "WindowsPowerShell\v1.0\Modules"
Import-Module (Join-Path $env:PSModulePath "Dism") -ErrorAction Stop
Import-Module (Join-Path $env:PSModulePath "PKI") -ErrorAction Stop
Import-Module (Join-Path $env:PSModulePath "Microsoft.PowerShell.Security") -ErrorAction Stop
$pnputil = Join-Path $sys "pnputil.exe"
$icacls = Join-Path $sys "icacls.exe"

# The driver version (DriverVer) comes from the manifest; it changes whenever the INF
# does (see DRIVER_RELEASES in tools/make_driver_manifest.py).
$DriverVersion = $null
# Our signer certificates, current and earlier names (the app was "ST25 Downloader").
$SignerPatterns = "*OpenEVP driver signer*", "*ST25 Downloader driver signer*"
function Is-OurSigner($c) { foreach ($p in $SignerPatterns) { if ($c.Subject -like $p) { return $true } }; return $false }

$logDir = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot "..\..\driver-setup"))
New-Item -ItemType Directory -Force $logDir | Out-Null
$log = Join-Path $logDir "setup.log"
function Log($msg) {
    try { Add-Content -Path $log -Value ("{0:HH:mm:ss} {1}" -f (Get-Date), $msg) } catch { }
}
Set-Content -Path $log -Value "OpenEVP WinUSB setup"

# 0 = installed; 259 = added, no matching device present yet; 3010 = restart needed
$okCodes = 0, 259, 3010
$restart = $false
function Install-Package($inf) {
    $out = & $pnputil /add-driver $inf /install 2>&1 | Out-String
    $code = $LASTEXITCODE
    Log "pnputil /add-driver $inf /install -> exit $code`n$out"
    if ($code -eq 3010) { $script:restart = $true }
    return $code
}
function Remove-Package($d) {
    $out = & $pnputil /delete-driver $d.Driver /uninstall /force 2>&1 | Out-String
    Log "removed package $($d.Driver) ($($d.Version)): exit $LASTEXITCODE`n$out"
    if ($LASTEXITCODE -eq 3010) { $script:restart = $true }
    return $LASTEXITCODE -in 0, 3010
}
function Our-Packages { @(Get-WindowsDriver -Online | Where-Object { $_.OriginalFileName -like "*\st25_winusb.inf" }) }

$cert = $null
$pkg = $null
$ok = $false
try {
    . (Join-Path $PSScriptRoot "manifest.ps1")
    $manifest = Read-DriverManifest (Join-Path $PSScriptRoot "models.json")
    $DriverVersion = $manifest.Version
    Log ("driver {0} for: {1}" -f $DriverVersion, (($manifest.Models | ForEach-Object {
        "$($_.name) ($(@($_.hardware_ids) -join ', '))" }) -join "; "))

    $ours = Our-Packages

    $current = $ours | Where-Object { [version]$_.Version -eq $DriverVersion } | Select-Object -First 1
    $installed = $false
    if ($current) {
        $installed = (Install-Package $current.OriginalFileName) -in $okCodes
        if ($installed) { Log "existing package $($current.Driver) installed" }
        else { Log "existing package $($current.Driver) could not be installed; building a new one" }
    }

    if (-not $installed) {
        $pkg = Join-Path $env:SystemRoot ("Temp\openevp-driver-" + [guid]::NewGuid().ToString("N"))
        New-Item -ItemType Directory $pkg | Out-Null
        & $icacls $pkg /inheritance:r /grant:r "*S-1-5-32-544:(OI)(CI)F" "*S-1-5-18:(OI)(CI)F" | Out-Null
        if ($LASTEXITCODE -ne 0) { throw "could not restrict access to $pkg" }
        if (@(Get-ChildItem -Force $pkg).Count -ne 0) { throw "$pkg was not empty" }
        $inf = Join-Path $pkg "st25_winusb.inf"
        $cat = Join-Path $pkg "st25_winusb.cat"

        Set-Content -Path $inf -Encoding ASCII -Value $manifest.Inf
        Log "wrote $inf"

        New-FileCatalog -Path $pkg -CatalogFilePath $cat -CatalogVersion 2 | Out-Null
        Log "catalog created"

        # No commas in the name: a comma makes Windows quote it (CN="..."). ca=false: this
        # certificate can sign files, never other certificates.
        $subject = "CN=OpenEVP driver signer - $env:COMPUTERNAME $(Get-Date -Format yyyy-MM-dd)"
        $cert = New-SelfSignedCertificate -Type CodeSigningCert -Subject $subject `
            -CertStoreLocation Cert:\LocalMachine\My -KeyExportPolicy NonExportable `
            -TextExtension @("2.5.29.19={critical}{text}ca=false") -NotAfter (Get-Date).AddYears(50)
        Log "certificate created: $($cert.Thumbprint) $subject"

        $cer = Join-Path $pkg "signer.cer"
        Export-Certificate -Cert $cert -FilePath $cer | Out-Null
        Import-Certificate -FilePath $cer -CertStoreLocation Cert:\LocalMachine\Root | Out-Null
        Import-Certificate -FilePath $cer -CertStoreLocation Cert:\LocalMachine\TrustedPublisher | Out-Null
        Log "certificate trusted (Root, TrustedPublisher)"

        $sig = Set-AuthenticodeSignature -FilePath $cat -Certificate $cert -HashAlgorithm SHA256
        Log "catalog signature: $($sig.Status) $($sig.StatusMessage)"
        if ($sig.Status -ne "Valid") { throw "catalog signature is $($sig.Status)" }

        Remove-Item -Path "Cert:\LocalMachine\My\$($cert.Thumbprint)" -DeleteKey
        Log "private key deleted"

        $code = Install-Package $inf
        if ($code -notin $okCodes) { throw "pnputil failed with exit code $code" }
    }

    # Only now that this version is in place: remove any other version of our package.
    foreach ($other in Our-Packages | Where-Object { [version]$_.Version -ne $DriverVersion }) {
        if (-not (Remove-Package $other)) { Log "warning: could not remove $($other.Driver)" }
    }

    # Keep only the certificates that signed a package still installed; any other of ours
    # (an interrupted run, a replaced version, the old name) is no longer needed.
    $needed = @{}
    foreach ($d in Our-Packages) {
        $catFile = Join-Path (Split-Path $d.OriginalFileName) "st25_winusb.cat"
        $signer = (Get-AuthenticodeSignature -FilePath $catFile).SignerCertificate
        if ($signer) { $needed[$signer.Thumbprint] = $true }
    }
    foreach ($store in "Root", "TrustedPublisher", "My") {
        foreach ($c in @(Get-ChildItem "Cert:\LocalMachine\$store" | Where-Object { (Is-OurSigner $_) -and -not $needed[$_.Thumbprint] })) {
            try { Remove-Item -Path $c.PSPath -DeleteKey -ErrorAction Stop; Log "removed unneeded certificate $($c.Thumbprint) from $store" }
            catch { Log "warning: could not remove certificate $($c.Thumbprint) from ${store}: $($_.Exception.Message)" }
        }
    }

    # pnputil succeeding does not prove the recorder uses WinUSB (a higher-ranked
    # driver would win): check every connected recorder of every model the driver covers,
    # each device on its own.
    $bad = @()
    $present = @(Get-PnpDevice -PresentOnly -ErrorAction SilentlyContinue)
    foreach ($hwid in @($manifest.Models | ForEach-Object { @($_.hardware_ids) })) {
        foreach ($dev in @($present | Where-Object { $_.InstanceId -like "$hwid\*" })) {
            $service = (Get-PnpDeviceProperty -InstanceId $dev.InstanceId -KeyName DEVPKEY_Device_Service).Data
            Log "connected recorder $($dev.InstanceId): service '$service', status $($dev.Status)"
            if ($service -ne "WINUSB" -and -not $restart) { $bad += "$($dev.InstanceId) uses '$service'" }
        }
    }
    if ($bad.Count) { throw "the recorder is not using WinUSB: $($bad -join '; ')" }
    $ok = $true
    Log $(if ($restart) { "DONE (restart needed)" } else { "DONE" })
} catch {
    Log "FAILED: $($_.Exception.Message)"
} finally {
    # Each cleanup step on its own, so one failure never skips the others: never leave
    # the private key behind, and never leave a trusted certificate that no installed
    # package needs.
    $ErrorActionPreference = "Continue"
    if ($cert) {
        $mine = "Cert:\LocalMachine\My\$($cert.Thumbprint)"
        try { if (Test-Path $mine) { Remove-Item -Path $mine -DeleteKey -ErrorAction Stop; Log "private key deleted (cleanup)" } }
        catch { Log "CLEANUP: could not delete the private key: $($_.Exception.Message)" }
        if (-not $ok) {
            foreach ($store in "Root", "TrustedPublisher") {
                $c = "Cert:\LocalMachine\$store\$($cert.Thumbprint)"
                try { if (Test-Path $c) { Remove-Item -Path $c -ErrorAction Stop; Log "certificate removed from $store (setup failed)" } }
                catch { Log "CLEANUP: could not remove the certificate from ${store}: $($_.Exception.Message)" }
            }
        }
    }
    try { if ($pkg -and (Test-Path $pkg)) { Remove-Item -Recurse -Force $pkg -ErrorAction Stop } }
    catch { Log "CLEANUP: could not remove ${pkg}: $($_.Exception.Message)" }
}
exit $(if (-not $ok) { 1 } elseif ($restart) { 3010 } else { 0 })
