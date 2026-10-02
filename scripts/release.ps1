<#
  Publie une nouvelle version : met à jour la version, commit, tag et push.
  La GitHub Action "Release" construit ensuite l'archive et crée la release.

  Usage : .\scripts\release.ps1 0.3.0
#>
param([Parameter(Mandatory = $true)][string]$Version)

$ErrorActionPreference = 'Stop'
if ($Version -notmatch '^\d+\.\d+\.\d+$') { throw "Version attendue au format X.Y.Z" }
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

if (git status --porcelain) { throw "Le dépôt contient des modifications non commitées." }

$init = 'backend/src/colabconnect/__init__.py'
$pyproject = 'backend/pyproject.toml'
$utf8 = New-Object System.Text.UTF8Encoding($false)
[IO.File]::WriteAllText("$root/$init", ((Get-Content $init -Raw) -replace '__version__ = "[^"]+"', "__version__ = `"$Version`""), $utf8)
[IO.File]::WriteAllText("$root/$pyproject", ((Get-Content $pyproject -Raw) -replace '(?m)^version = "[^"]+"', "version = `"$Version`""), $utf8)

Push-Location backend
uv lock
Pop-Location

git add $init $pyproject backend/uv.lock
git commit -m "Release v$Version"
git tag -a "v$Version" -m "v$Version"
git push origin HEAD "v$Version"
Write-Host "Tag v$Version poussé : la release sera publiée par GitHub Actions."
