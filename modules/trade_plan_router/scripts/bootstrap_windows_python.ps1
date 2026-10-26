$ErrorActionPreference = 'Stop'

function Test-PythonLauncher {
    param(
        [Parameter(Mandatory = $true)][string]$Exe,
        [string[]]$PrefixArgs = @()
    )

    & $Exe @PrefixArgs -c "import sys; print(sys.version); raise SystemExit(0 if sys.version_info >= (3, 12) else 1)"
    return $LASTEXITCODE -eq 0
}

$launchers = [System.Collections.Generic.List[object]]::new()
foreach ($name in @('python3.14', 'python3.13', 'python3.12', 'python3', 'python')) {
    $command = Get-Command $name -ErrorAction SilentlyContinue
    if ($null -ne $command) {
        $launchers.Add([pscustomobject]@{ Exe = $command.Source; Args = @() })
    }
}

$py = Get-Command py -ErrorAction SilentlyContinue
if ($null -ne $py) {
    foreach ($selector in @('-3.14', '-3.13', '-3.12')) {
        $launchers.Add([pscustomobject]@{ Exe = $py.Source; Args = @($selector) })
    }
}

foreach ($root in @(
    $env:RUNNER_TOOL_CACHE,
    $env:RUNNER_TEMP,
    (Join-Path $env:LOCALAPPDATA 'Programs\Python'),
    'C:\Python314',
    'C:\Python313',
    'C:\Python312',
    'C:\Program Files\Python314',
    'C:\Program Files\Python313',
    'C:\Program Files\Python312'
)) {
    if ([string]::IsNullOrWhiteSpace($root) -or -not (Test-Path $root)) {
        continue
    }
    Get-ChildItem -Path $root -Filter python.exe -File -Recurse -ErrorAction SilentlyContinue |
        ForEach-Object {
            $launchers.Add([pscustomobject]@{ Exe = $_.FullName; Args = @() })
        }
}

$selected = $null
$seen = @{}
foreach ($launcher in $launchers) {
    $exe = [string]$launcher.Exe
    $prefix = @($launcher.Args)
    $key = "$exe|$($prefix -join ' ')"
    if ($seen.ContainsKey($key)) {
        continue
    }
    $seen[$key] = $true
    try {
        if (Test-PythonLauncher -Exe $exe -PrefixArgs $prefix) {
            $selected = $launcher
            break
        }
    }
    catch {
        Write-Host "Ignoring unusable Python candidate: $key"
    }
}

if ($null -eq $selected) {
    $version = '3.12.10'
    $portableRoot = Join-Path $env:RUNNER_TEMP "router-python-$version"
    $packagePath = Join-Path $env:RUNNER_TEMP "python-$version.nupkg"
    $packageUri = "https://www.nuget.org/api/v2/package/python/$version"

    Remove-Item -LiteralPath $portableRoot -Recurse -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $packagePath -Force -ErrorAction SilentlyContinue
    Write-Host "Downloading portable Python $version from NuGet"
    Invoke-WebRequest -Uri $packageUri -OutFile $packagePath -UseBasicParsing
    Expand-Archive -LiteralPath $packagePath -DestinationPath $portableRoot -Force

    $portablePython = Join-Path $portableRoot 'tools\python.exe'
    if (-not (Test-Path $portablePython)) {
        throw "Portable Python executable not found: $portablePython"
    }
    if (-not (Test-PythonLauncher -Exe $portablePython)) {
        throw 'Downloaded portable Python is not usable'
    }
    $selected = [pscustomobject]@{ Exe = $portablePython; Args = @() }
}

$selectedExe = [string]$selected.Exe
$selectedArgs = @($selected.Args)
Write-Host "Selected Python: $selectedExe $($selectedArgs -join ' ')"
& $selectedExe @selectedArgs -m venv .venv-router-validation
if ($LASTEXITCODE -ne 0) {
    throw 'Failed to create Windows validation venv'
}

$venv = Join-Path $PWD '.venv-router-validation\Scripts'
$venvPython = Join-Path $venv 'python.exe'
if (-not (Test-Path $venvPython)) {
    throw "Validation venv Python not found: $venvPython"
}
Add-Content -Path $env:GITHUB_PATH -Value $venv
& $venvPython --version
