# Build Diskclosure.exe from this folder on Windows.
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

python -m pip install -e ".[build]"
pyinstaller --noconfirm --clean Diskclosure.spec

Write-Host "Built $($PSScriptRoot)\dist\Diskclosure.exe"
