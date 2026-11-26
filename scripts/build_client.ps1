$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $PSScriptRoot
$uv = (Get-Command uv -CommandType Application).Source
$node = (Get-Command node -CommandType Application | Select-Object -First 1).Source
$npm = (Get-Command npm.cmd -CommandType Application | Select-Object -First 1).Source
$originalPath = $env:Path
Push-Location -LiteralPath $projectRoot
try {
    foreach ($outputDirectory in @("dist", "dist/Story")) {
        if (Test-Path -LiteralPath $outputDirectory) {
            $directory = Get-Item -LiteralPath $outputDirectory -Force
            if (-not $directory.PSIsContainer -or
                ($directory.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
                throw "Unsafe build output directory: $outputDirectory"
            }
        }
    }
    if (Test-Path -LiteralPath "dist/Story/data") {
        throw "Move the campaign data out of dist/Story/data before rebuilding."
    }

    $nodeVersion = & $node -p "process.versions.node"
    if ($LASTEXITCODE -ne 0 -or $nodeVersion -notmatch '^\d+\.\d+\.\d+$' -or [int]($nodeVersion.Split('.')[0]) -lt 22) {
        throw "Building Story requires Node.js 22 or later."
    }
    & $npm ci --prefix dice_component --omit=dev --ignore-scripts --no-audit --no-fund
    if ($LASTEXITCODE -ne 0) {
        throw "Installing the dice engine failed."
    }
    $nodeLicense = Join-Path (Split-Path -Parent $node) "LICENSE"
    if (-not (Test-Path -LiteralPath $nodeLicense)) {
        New-Item -ItemType Directory -Path build -Force | Out-Null
        $nodeLicense = "build/node-$nodeVersion-LICENSE.txt"
        if (-not (Test-Path -LiteralPath $nodeLicense)) {
            Invoke-WebRequest -Uri "https://raw.githubusercontent.com/nodejs/node/v$nodeVersion/LICENSE" -OutFile $nodeLicense
        }
    }

    $buildOptions = @(
        "--onedir"
        "--windowed"
        "--noconfirm"
        "--name", "Story"
        "--paths", "."
        "--collect-submodules", "uvicorn"
        "--hidden-import", "websockets.asyncio.server"
        "--specpath", "build"
        "--workpath", "build/windows-client"
        "--distpath", "dist"
        "--add-data", "$projectRoot/assets/icons/phosphor;assets/icons/phosphor"
        "--add-data", "$projectRoot/assets/icons/story;assets/icons/story"
        "--add-data", "$projectRoot/dice_component/worker.cjs;dice_component"
        "--add-data", "$projectRoot/dice_component/node_modules;dice_component/node_modules"
        "client/__main__.py"
    )
    # Restrict dependency discovery to Windows and the project environment.
    $env:Path = @([Environment]::SystemDirectory, $env:SystemRoot) -join ";"
    & $uv run --locked --group build pyinstaller @buildOptions
    if ($LASTEXITCODE -ne 0) {
        throw "PyInstaller failed with exit code $LASTEXITCODE."
    }

    Copy-Item -LiteralPath $node -Destination "dist/Story/_internal/dice_component/node.exe"
    Copy-Item -LiteralPath $nodeLicense -Destination "dist/Story/_internal/dice_component/NODE-LICENSE.txt"

    $archive = "dist/Story-Windows.zip"
    Compress-Archive -LiteralPath "dist/Story" -DestinationPath $archive -Force
    Write-Output (Resolve-Path -LiteralPath $archive).Path
}
finally {
    $env:Path = $originalPath
    Pop-Location
}
