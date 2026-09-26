# Build the OpenEVP app (dist\OpenEVP\), the ST25 command-line tool (dist\openevp-st25.exe)
# and the installer (dist\OpenEVP-Setup-<version>.exe).
#   powershell -ExecutionPolicy Bypass -File build_windows.ps1
# A build without the LPEC table data (no WAV conversion) must be asked for:
#   powershell -ExecutionPolicy Bypass -File build_windows.ps1 -NoLpecTables
param([switch]$NoLpecTables)
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
$dll = "vendor\libusb-1.0.30\libusb-1.0.dll"
# SHA-256 of the MinGW64 DLL from the official, PGP-signed libusb-1.0.30.7z release
# (signed by Tormod Volden, key 9C7EA94939C69C4FBC3DBFA8AA0639079EFB61B9).
$expected = "5bd409849825009b6fe25861a6147f76d256aab248f07049d78387e3bff12d94"
$actual = (Get-FileHash $dll -Algorithm SHA256).Hash.ToLower()
if ($actual -ne $expected) { throw "libusb-1.0.dll hash mismatch: $actual" }

# The bundled DLL is x64, so the Python that PyInstaller freezes (and therefore
# the .exe) must be x64 too: a 32-bit or ARM64 build would fail at run time
# with "not a valid Win32 application" when loading libusb.
function Get-PeMachine($path) {
    $bytes = [System.IO.File]::ReadAllBytes((Resolve-Path $path))
    $pe = [BitConverter]::ToInt32($bytes, 0x3C)
    if ([Text.Encoding]::ASCII.GetString($bytes, $pe, 4) -ne "PE`0`0") { throw "$path is not a PE file" }
    return [BitConverter]::ToUInt16($bytes, $pe + 4)
}
$AMD64 = 0x8664
if ((Get-PeMachine $dll) -ne $AMD64) { throw "libusb-1.0.dll is not an x64 DLL" }
$pyplat = python -c "import sysconfig; print(sysconfig.get_platform())"
if ($LASTEXITCODE -ne 0) { throw "python not found" }
if ($pyplat -ne "win-amd64") { throw "Python is '$pyplat'; an x64 (win-amd64) Python is required to match libusb-1.0.dll" }

# The LPEC decoder's C core (st25\lpec\lpec_core.dll): built before the tests so
# they check it against pure Python, and collected into the app below by
# --collect-binaries st25.lpec. The pure-Python decoder still works without
# it (~60x slower), but that is a fallback for a dev machine without gcc, not
# an acceptable release: once _lpec.c exists, a build that can't produce the
# DLL throws and stops here rather than silently shipping the slow decoder.
if (Test-Path st25\lpec\_lpec.c) {
    python tools\build_lpec_core.py
    if ($LASTEXITCODE -ne 0) { throw "building lpec_core.dll failed" }
    if ((Get-PeMachine "st25\lpec\lpec_core.dll") -ne $AMD64) { throw "lpec_core.dll is not an x64 DLL" }
}

# st25.lpec is imported dynamically (st25/audio.py), so PyInstaller cannot see it:
# collect it explicitly for both builds below, or the frozen app/CLI silently
# lose WAV support. The extracted table data (st25/lpec/data/lpec_tables.json,
# generated locally by tools/import_lpec_tables.py and never committed) is
# bundled the same way. Without it the built app and CLI look fine but can never
# convert to WAV, so its absence fails the build unless -NoLpecTables asks for
# such a build (for development).
$decoder = @()
if (Test-Path st25\lpec) {
    $decoder = @("--collect-submodules", "st25.lpec", "--collect-binaries", "st25.lpec")
    $lpecTables = "st25\lpec\data\lpec_tables.json"
    if (Test-Path $lpecTables) {
        $decoder += @("--add-data", "$lpecTables;st25\lpec\data")
    } elseif ($NoLpecTables) {
        Write-Warning "$lpecTables not found (-NoLpecTables): the built app and CLI will NOT be able to convert to WAV."
    } else {
        throw "$lpecTables not found: the built app and CLI could not convert to WAV. Run tools\import_lpec_tables.py and rebuild, or pass -NoLpecTables for a development build without WAV conversion."
    }
}

python -m unittest discover -s tests
if ($LASTEXITCODE -ne 0) { throw "tests failed" }
python -m PyInstaller --noconfirm --clean --onefile --console --name openevp-st25 `
    --icon assets\st25.ico --add-binary "$dll;." @decoder st25-download.py
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed" }
if ((Get-PeMachine "dist\openevp-st25.exe") -ne $AMD64) { throw "the built .exe is not x64" }
Get-FileHash dist\openevp-st25.exe -Algorithm SHA256

# Desktop app: one-folder build (starts faster and trips antivirus less than one-file).
python -m pip install -r requirements-app.txt
if ($LASTEXITCODE -ne 0) { throw "pip install failed" }
python -m PyInstaller --noconfirm --clean --onedir --windowed --name "OpenEVP" `
    --icon assets\st25.ico --add-binary "$dll;." --add-data "app/ui;app/ui" `
    --add-data "assets/st25.ico;assets" --add-data "app/driver;driver" @decoder st25-app.py
if ($LASTEXITCODE -ne 0) { throw "PyInstaller (app) failed" }
if ((Get-PeMachine "dist\OpenEVP\OpenEVP.exe") -ne $AMD64) { throw "the built app is not x64" }

# All-in-one installer: app + command-line tool + recorder driver + WebView2 if missing.
$wv2 = "vendor\webview2\MicrosoftEdgeWebview2Setup.exe"
# SHA-256 of Microsoft's Evergreen WebView2 bootstrapper 1.3.271.7 (Authenticode: Microsoft Corporation).
$wv2Expected = "81c01751c8cc385a5991abb104205d42ac70094350ee8fb9e8ea580b51bb9554"
if ((Get-FileHash $wv2 -Algorithm SHA256).Hash.ToLower() -ne $wv2Expected) { throw "WebView2 bootstrapper hash mismatch" }
$version = python -c "import st25; print(st25.__version__)"
$iscc = @("$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe", "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
          "$env:ProgramFiles\Inno Setup 6\ISCC.exe") | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $iscc) { throw "Inno Setup 6 (ISCC.exe) not found: winget install JRSoftware.InnoSetup" }
& $iscc /Q "/DAppVersion=$version" installer\openevp.iss
if ($LASTEXITCODE -ne 0) { throw "Inno Setup failed" }
Get-FileHash "dist\OpenEVP-Setup-$version.exe" -Algorithm SHA256
