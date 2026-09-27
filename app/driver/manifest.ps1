# Read-DriverManifest: load and check the driver manifest (models.json), dot-sourced by
# install-winusb.ps1. models.json is written at build time by tools/make_driver_manifest.py
# from the recorder registry and installed next to this file (Program Files, admin-only):
# the recorder models the driver covers, their USB hardware ids, the driver version and
# the INF text itself. Anything unexpected in it stops the setup instead of guessing.

function Read-DriverManifest([string]$Path) {
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw "the driver manifest $Path is missing (this build is incomplete)"
    }
    $m = Get-Content -LiteralPath $Path -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($m.schema -ne 1) { throw "driver manifest: unknown schema '$($m.schema)'" }
    if ([string]$m.driver_version -notmatch '^\d{1,5}\.\d{1,5}\.\d{1,5}\.\d{1,5}$') {
        throw "driver manifest: bad driver_version '$($m.driver_version)'"
    }
    if ([string]$m.driver_date -notmatch '^\d\d/\d\d/\d{4}$') {
        throw "driver manifest: bad driver_date '$($m.driver_date)'"
    }
    $models = @($m.models)
    if ($models.Count -eq 0) { throw "driver manifest: no recorder model" }
    $ids = @{}
    foreach ($model in $models) {
        foreach ($field in "model_id", "name") {
            if ([string]$model.$field -cnotmatch '^[ !#$&-~]{1,120}\z') { throw "driver manifest: bad model $field '$($model.$field)'" }
        }
        $hw = @($model.hardware_ids)
        if ($hw.Count -eq 0) { throw "driver manifest: $($model.model_id) has no hardware id" }
        foreach ($h in $hw) {
            if ([string]$h -cnotmatch '^USB\\VID_[0-9A-F]{4}&PID_[0-9A-F]{4}\z') { throw "driver manifest: bad hardware id '$h'" }
            $ids[[string]$h] = $true
        }
    }

    # The INF, line by line: the fixed template (tools/make_driver_manifest.py inf_text)
    # with only device lines in the three [Devices.*] sections and only device-name
    # strings after Provider in [Strings]. Anything else (another section, directive or
    # spelling) stops the setup.
    $inf = [string]$m.inf
    if (-not $inf) { throw "driver manifest: the INF is empty" }
    $device = '^%(DeviceName\d*)% = USB_Install, (USB\\VID_[0-9A-F]{4}&PID_[0-9A-F]{4})\z'
    $string = '^(DeviceName\d*) = "[ !#$&-~]{1,120}"\z'
    $template = @(
        '[Version]', 'Signature   = "$Windows NT$"', 'Class       = USBDevice',
        'ClassGUID   = {88BAE032-5A81-49f0-BC3D-A4FF138216D6}', 'Provider    = %Provider%',
        'CatalogFile = st25_winusb.cat', "DriverVer   = $($m.driver_date),$($m.driver_version)", '',
        '[Manufacturer]', '%Provider% = Devices, NTamd64, NTx86, NTarm64', '',
        '[Devices.NTamd64]', '<devices>', '', '[Devices.NTx86]', '<devices>', '', '[Devices.NTarm64]', '<devices>', '',
        '[USB_Install]', 'Include = winusb.inf', 'Needs   = WINUSB.NT', '',
        '[USB_Install.Services]', 'Include = winusb.inf', 'Needs   = WINUSB.NT.Services', '',
        '[USB_Install.HW]', 'AddReg = Dev_AddReg', '',
        '[Dev_AddReg]', 'HKR,,DeviceInterfaceGUIDs,0x10000,"{7C3C3F6B-2E54-4F0B-9C1B-5D2A8B3E0A25}"', '',
        '[Strings]', 'Provider   = "OpenEVP"', '<strings>')
    $skeleton = New-Object System.Collections.Generic.List[string]
    $lists = New-Object System.Collections.Generic.List[string]     # each [Devices.*] section's lines
    $keys = @{}
    foreach ($line in $inf -split "`r`n") {
        if ($line -cmatch '[^\x20-\x7E]') { throw "driver manifest: the INF has a line that is not plain ASCII text" }
        if ($line -cmatch $device) {
            if ($skeleton.Count -and $skeleton[$skeleton.Count - 1] -eq '<devices>') { $lists[$lists.Count - 1] += "`n$line" }
            else { $skeleton.Add('<devices>'); $lists.Add($line) }
        } elseif ($line -cmatch $string) {
            if ($keys.ContainsKey($Matches[1])) { throw "driver manifest: the INF names $($Matches[1]) twice" }
            $keys[$Matches[1]] = $true
            if (-not ($skeleton.Count -and $skeleton[$skeleton.Count - 1] -eq '<strings>')) { $skeleton.Add('<strings>') }
        } else {
            $skeleton.Add($line)
        }
    }
    if (($skeleton -join "`n") -cne ($template -join "`n")) {
        throw "driver manifest: the INF is not the driver template (sections, directives or names changed)"
    }
    if ($lists[1] -cne $lists[0] -or $lists[2] -cne $lists[0]) { throw "driver manifest: the INF's [Devices.*] sections differ" }
    # The INF binds exactly the manifest's hardware ids (so the check after installing
    # covers every device the driver can take), each under a name it defines.
    $inInf = @{}
    foreach ($line in $lists[0] -split "`n") {
        [void]($line -cmatch $device)
        if (-not $keys.ContainsKey($Matches[1])) { throw "driver manifest: the INF does not define $($Matches[1])" }
        $inInf[$Matches[2]] = $true
    }
    foreach ($h in $inInf.Keys) { if (-not $ids[$h]) { throw "driver manifest: the INF binds $h, which no model lists" } }
    foreach ($h in $ids.Keys) { if (-not $inInf[$h]) { throw "driver manifest: the INF does not bind $h" } }

    [pscustomobject]@{
        Version = [version]$m.driver_version
        Date    = [string]$m.driver_date
        Models  = $models
        Inf     = $inf
    }
}
