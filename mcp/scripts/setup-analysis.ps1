param([string]$Directory = "")
$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
$repoRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot "../.."))
if (-not $Directory) { $Directory = Join-Path $repoRoot "build/analysis-tools" }
$Directory = [IO.Path]::GetFullPath($Directory)

# Optional local dependencies only. No installer, global PATH change, or
# modification of an existing Ghidra installation is performed.
$packages = @(
    @{
        Archive = "OpenJDK21U-jdk_x64_windows_hotspot_21.0.12.1_1.zip"
        Url = "https://github.com/adoptium/temurin21-binaries/releases/download/jdk-21.0.12.1%2B1/OpenJDK21U-jdk_x64_windows_hotspot_21.0.12.1_1.zip"
        Sha256 = "f9d6e191ab098c0d416e7d588a24420a8621cd2f4720dab2459b8b7b2d2d8b4e"
        Folder = "jdk-21.0.12.1+1"
        Marker = "bin/java.exe"
        Variable = "JAVA_HOME"
    },
    @{
        Archive = "ghidra_12.1.3_PUBLIC_20260817.zip"
        Url = "https://github.com/NationalSecurityAgency/ghidra/releases/download/Ghidra_12.1.3_build/ghidra_12.1.3_PUBLIC_20260817.zip"
        Sha256 = "93a5d11a9ad510622acaaf908c556a7b9b764d338e78a7567f3689bf5081fd54"
        Folder = "ghidra_12.1.3_PUBLIC"
        Marker = "Ghidra/application.properties"
        Variable = "GHIDRA_HOME"
    }
)

New-Item -ItemType Directory -Path $Directory -Force | Out-Null
foreach ($package in $packages) {
    $archive = Join-Path $Directory $package.Archive
    $destination = Join-Path $Directory $package.Folder
    if (-not (Test-Path -LiteralPath $archive)) {
        Write-Output "Downloading official package: $($package.Archive)"
        Invoke-WebRequest -Uri $package.Url -OutFile $archive
    }
    $actualHash = (Get-FileHash -LiteralPath $archive -Algorithm SHA256).Hash
    if ($actualHash -ne $package.Sha256) {
        throw "SHA256 mismatch for $archive. No extraction performed; inspect the archive before retrying."
    }
    if (-not (Test-Path -LiteralPath $destination)) {
        Expand-Archive -LiteralPath $archive -DestinationPath $Directory
    }
    if (-not (Test-Path -LiteralPath (Join-Path $destination $package.Marker))) {
        throw "Incomplete dependency directory: $destination. Use a new -Directory; this script does not overwrite existing installations."
    }
    Write-Output "$($package.Variable)=$destination"
}
Write-Output "Ready. Set these environment values in the MCP server configuration; no global environment was changed."
