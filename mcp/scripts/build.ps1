param(
    [switch]$NoPython,
    [string]$Configuration = "Release",
    [string]$Python = "python",
    [string]$BuildDirectory = "",
    [switch]$EnableJit,
    [switch]$ConfigureOnly
)
$ErrorActionPreference = "Stop"
$repoRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot "../.."))
if (-not $BuildDirectory) { $BuildDirectory = Join-Path $repoRoot "build/mcp-direct" }
$vswhere = Join-Path ([Environment]::GetFolderPath("ProgramFilesX86")) "Microsoft Visual Studio/Installer/vswhere.exe"
$visualStudio = $null
if (Test-Path -LiteralPath $vswhere) {
    $visualStudio = & $vswhere -latest -products '*' -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath
}
if (-not (Get-Command cl.exe -ErrorAction SilentlyContinue)) {
    if (-not $visualStudio) { throw "Install Visual Studio C++ Build Tools or run from an existing C++ Developer PowerShell." }
    $vcvars = Join-Path $visualStudio "VC/Auxiliary/Build/vcvars64.bat"
    # Import the existing compiler environment into this process; do not open a
    # visible shell, install a toolchain, or print the environment to the log.
    $environmentCommand = 'call "' + $vcvars + '" >nul && set'
    $compilerEnvironment = & $env:ComSpec /d /c $environmentCommand
    if ($LASTEXITCODE -ne 0) { throw "Visual Studio environment initialization failed" }
    $compilerPath = $null
    foreach ($entry in $compilerEnvironment) {
        if ($entry -match '^([^=]+)=(.*)$') {
            $entryName = $Matches[1]
            $entryValue = $Matches[2]
            # Some PowerShell hosts hand cmd both PATH and Path. vcvars updates
            # uppercase PATH; do not overwrite it with the inherited stale Path.
            if ($entryName -ieq 'PATH') {
                if (-not $compilerPath -or $entryName -ceq 'PATH') { $compilerPath = $entryValue }
                continue
            }
            [Environment]::SetEnvironmentVariable($entryName, $entryValue, 'Process')
        }
    }
    if ($compilerPath) { [Environment]::SetEnvironmentVariable('Path', $compilerPath, 'Process') }
}
$cmakeCommand = Get-Command cmake -ErrorAction SilentlyContinue
$cmake = if ($cmakeCommand) { $cmakeCommand.Source } elseif ($visualStudio) {
    Join-Path $visualStudio "Common7/IDE/CommonExtensions/Microsoft/CMake/CMake/bin/cmake.exe"
}
$ninjaCommand = Get-Command ninja -ErrorAction SilentlyContinue
$ninja = if ($ninjaCommand) { $ninjaCommand.Source } elseif ($visualStudio) {
    Join-Path $visualStudio "Common7/IDE/CommonExtensions/Microsoft/CMake/Ninja/ninja.exe"
}
if (-not $cmake -or -not (Test-Path -LiteralPath $cmake)) { throw "CMake was not found in PATH or Visual Studio." }
if (-not $ninja -or -not (Test-Path -LiteralPath $ninja)) { throw "Ninja was not found in PATH or Visual Studio." }
$jit = if ($EnableJit) { 'ON' } else { 'OFF' }
& $cmake -S (Join-Path $repoRoot "mcp") -B $BuildDirectory -G Ninja "-DCMAKE_MAKE_PROGRAM=$ninja" "-DCMAKE_BUILD_TYPE=$Configuration" "-DENABLE_JIT=$jit" -DENABLE_OGLRENDERER=OFF -DENABLE_GDBSTUB=OFF
if ($LASTEXITCODE -ne 0) { throw "CMake configure failed" }
if ($ConfigureOnly) { return }
& $cmake --build $BuildDirectory --config $Configuration --parallel
if ($LASTEXITCODE -ne 0) { throw "Native build failed" }
if (-not $NoPython) {
    $environmentDirectory = Join-Path $repoRoot "mcp/.venv"
    if (-not (Test-Path -LiteralPath (Join-Path $environmentDirectory "Scripts/python.exe"))) {
        & $Python -m venv $environmentDirectory
        if ($LASTEXITCODE -ne 0) { throw "Virtual environment creation failed" }
    }
    & (Join-Path $environmentDirectory "Scripts/python.exe") -m pip install -r (Join-Path $repoRoot "mcp/python/requirements.txt")
    if ($LASTEXITCODE -ne 0) { throw "Python dependency installation failed" }
}
