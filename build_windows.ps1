# Build the OpenEVP app (dist\OpenEVP\), the ST25 command-line tool (dist\openevp-st25.exe)
# and the installer (dist\OpenEVP-Setup-<version>.exe).
#   powershell -ExecutionPolicy Bypass -File build_windows.ps1
# A build without the LPEC (LP, SP) and LPEC ST table data (no WAV conversion) must be asked for:
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

# The Sony decoders' C cores (openevp\decoders\sony_lpec\lpec_core.dll for the
# ICD-ST25's LPEC LP, openevp\decoders\sony_lpec_st\lpec_st_core.dll for the
# ICD-ST10's LPEC ST): built before the tests so they check them against pure
# Python, and collected into both builds below. The pure-Python decoders still
# work without them (15-60x slower), but that is a fallback for a dev machine
# without gcc, not an acceptable release: a build that can't produce the DLLs
# throws and stops here rather than silently shipping the slow decoders.
$lpecDir = "openevp\decoders\sony_lpec"
$lpecCore = "$lpecDir\lpec_core.dll"
$lpecStDir = "openevp\decoders\sony_lpec_st"
$lpecStCore = "$lpecStDir\lpec_st_core.dll"
# The MP3 decoder's C core (openevp\decoders\mp3\mp3_core.dll, minimp3 from
# vendor\minimp3, pinned by SHA-256 in the tool) has no pure-Python fallback at all.
$mp3Core = "openevp\decoders\mp3\mp3_core.dll"
python tools\build_lpec_core.py
if ($LASTEXITCODE -ne 0) { throw "building the decoders' C cores failed" }
foreach ($core in $lpecCore, $lpecStCore, $mp3Core) {
    if (-not (Test-Path $core)) { throw "$core was not built" }
    if ((Get-PeMachine $core) -ne $AMD64) { throw "$core is not an x64 DLL" }
}

# The decoders (openevp.decoders.sony_lpec and openevp.decoders.sony_lpec_st) are
# imported dynamically (sony_icd/audio.py), so PyInstaller cannot see them: the
# openevp package is collected explicitly, with the decoders' DLLs, for both
# builds below, or the frozen app/CLI silently lose WAV support. The extracted
# table data ($lpecDir\data\lpec_tables.json and $lpecDir\data\lpec_sp_tables.json
# from tools/import_lpec_tables.py, $lpecStDir\data\lpec_st_tables.json from
# tools/import_lpec_st_tables.py; all generated locally and never committed) is
# bundled the same way. Without it the
# built app and CLI look fine but can never convert to WAV (ICD-ST25 or ICD-ST10
# recordings), so its absence fails the build unless -NoLpecTables asks for such
# a build (for development).
$decoder = @("--collect-submodules", "openevp", "--collect-binaries", "openevp.decoders.sony_lpec",
             "--collect-binaries", "openevp.decoders.sony_lpec_st",
             "--collect-binaries", "openevp.decoders.mp3")
$tables = @(@{ File = "$lpecDir\data\lpec_tables.json"; Dir = "$lpecDir\data"; Tool = "tools\import_lpec_tables.py";
               What = "ICD-ST25 (LPEC LP)" },
            @{ File = "$lpecDir\data\lpec_sp_tables.json"; Dir = "$lpecDir\data"; Tool = "tools\import_lpec_tables.py";
               What = "ICD-ST10 SP mode (LPEC SP)" },
            @{ File = "$lpecStDir\data\lpec_st_tables.json"; Dir = "$lpecStDir\data"; Tool = "tools\import_lpec_st_tables.py";
               What = "ICD-ST10 (LPEC ST)" })
foreach ($t in $tables) {
    if (Test-Path $t.File) {
        $decoder += @("--add-data", "$($t.File);$($t.Dir)")
    } elseif ($NoLpecTables) {
        Write-Warning "$($t.File) not found (-NoLpecTables): the built app and CLI will NOT be able to convert $($t.What) recordings to WAV."
    } else {
        throw "$($t.File) not found: the built app and CLI could not convert $($t.What) recordings to WAV. Run $($t.Tool) and rebuild, or pass -NoLpecTables for a development build without WAV conversion."
    }
}

# The driver manifest (app\driver\models.json, a build output): which recorder models
# the WinUSB driver covers, their USB ids, the driver version and the INF, from the
# recorder registry. Bundled with the driver scripts below; fails if the INF changed
# without a new driver version (DRIVER_RELEASES in the tool). Regenerated before the
# tests, so a registry/INF change never leaves them (or the build) on a stale one.
python tools\make_driver_manifest.py
if ($LASTEXITCODE -ne 0) { throw "making the driver manifest failed" }

# The app's requirements (pywebview, numpy, lameenc for MP3 clips) before the tests:
# the release gate fails the MP3 clip tests without lameenc.
python -m pip install -r requirements-app.txt
if ($LASTEXITCODE -ne 0) { throw "pip install failed" }

# The release gate (tests/release_gate.py): the decoder's golden tests must run,
# so missing or damaged tables or a DLL that doesn't load fail the tests instead
# of skipping them. Not for a -NoLpecTables development build, which has no tables.
if (-not $NoLpecTables) { $env:OPENEVP_RELEASE_GATE = "1" }
try {
    python -m unittest discover -s tests
    if ($LASTEXITCODE -ne 0) { throw "tests failed" }
} finally {
    Remove-Item Env:\OPENEVP_RELEASE_GATE -ErrorAction SilentlyContinue
}
python -m PyInstaller --noconfirm --clean --onefile --console --name openevp-st25 `
    --icon assets\st25.ico --add-binary "$dll;." @decoder st25-download.py
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed" }
if ((Get-PeMachine "dist\openevp-st25.exe") -ne $AMD64) { throw "the built .exe is not x64" }
Get-FileHash dist\openevp-st25.exe -Algorithm SHA256

# Desktop app: one-folder build (starts faster and trips antivirus less than one-file).
# lameenc (MP3 clips, openevp.mp3; LGPL; installed before the tests above) is an
# extension module: named as a hidden import so a build never silently loses MP3
# export, with its metadata (its version for --smoke, and its license file).
python -m PyInstaller --noconfirm --clean --onedir --windowed --name "OpenEVP" `
    --icon assets\st25.ico --add-binary "$dll;." --add-data "app/ui;app/ui" `
    --add-data "assets/st25.ico;assets" --add-data "app/driver;driver" --add-data "LICENSES;LICENSES" `
    --hidden-import lameenc --copy-metadata lameenc @decoder st25-app.py
if ($LASTEXITCODE -ne 0) { throw "PyInstaller (app) failed" }
if ((Get-PeMachine "dist\OpenEVP\OpenEVP.exe") -ne $AMD64) { throw "the built app is not x64" }
foreach ($f in "install-winusb.ps1", "uninstall-winusb.ps1", "manifest.ps1", "models.json") {
    $built = "dist\OpenEVP\_internal\driver\$f"
    if (-not (Test-Path $built)) { throw "the built app has no $built" }
    if ((Get-FileHash $built).Hash -ne (Get-FileHash "app\driver\$f").Hash) { throw "$built differs from app\driver\$f" }
}

# All-in-one installer: app + command-line tool + recorder driver + WebView2 if missing.
$wv2 = "vendor\webview2\MicrosoftEdgeWebview2Setup.exe"
# SHA-256 of Microsoft's Evergreen WebView2 bootstrapper 1.3.271.7 (Authenticode: Microsoft Corporation).
$wv2Expected = "81c01751c8cc385a5991abb104205d42ac70094350ee8fb9e8ea580b51bb9554"
if ((Get-FileHash $wv2 -Algorithm SHA256).Hash.ToLower() -ne $wv2Expected) { throw "WebView2 bootstrapper hash mismatch" }
$version = python -c "import openevp; print(openevp.__version__)"
$iscc = @("$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe", "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
          "$env:ProgramFiles\Inno Setup 6\ISCC.exe") | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $iscc) { throw "Inno Setup 6 (ISCC.exe) not found: winget install JRSoftware.InnoSetup" }
& $iscc /Q "/DAppVersion=$version" installer\openevp.iss
if ($LASTEXITCODE -ne 0) { throw "Inno Setup failed" }
Get-FileHash "dist\OpenEVP-Setup-$version.exe" -Algorithm SHA256
