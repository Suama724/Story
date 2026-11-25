$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $PSScriptRoot
$uv = (Get-Command uv -CommandType Application).Source
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
        "client/__main__.py"
    )
    # Restrict dependency discovery to Windows and the project environment.
    $env:Path = @([Environment]::SystemDirectory, $env:SystemRoot) -join ";"
    & $uv run --locked --group build pyinstaller @buildOptions
    if ($LASTEXITCODE -ne 0) {
        throw "PyInstaller failed with exit code $LASTEXITCODE."
    }

    $archive = "dist/Story-Windows.zip"
    Compress-Archive -LiteralPath "dist/Story" -DestinationPath $archive -Force
    Write-Output (Resolve-Path -LiteralPath $archive).Path
}
finally {
    $env:Path = $originalPath
    Pop-Location
}
