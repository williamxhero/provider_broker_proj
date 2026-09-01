param([string]$HostName = "yosef-server")
$ErrorActionPreference = "Stop"
$Project = Split-Path -Parent $PSScriptRoot
$Version = python -c "import tomllib; print(tomllib.load(open('pyproject.toml','rb'))['project']['version'])"
if ($LASTEXITCODE -ne 0 -or -not $Version) { throw "Could not read project version" }

python -m build --wheel
if ($LASTEXITCODE -ne 0) { throw "Wheel build failed" }
$Wheel = Join-Path $Project "dist\provider_broker-$Version-py3-none-any.whl"
if (-not (Test-Path -LiteralPath $Wheel)) { throw "Expected wheel not found: $Wheel" }

$Stage = "/data/provider-broker/stage/$Version"
ssh $HostName "sudo -n install -d -o yosef -g yosef '$Stage'"
if ($LASTEXITCODE -ne 0) { throw "Could not create remote staging directory" }
scp $Wheel "$Project\scripts\install.sh" "$Project\scripts\smoke.py" "$Project\scripts\transport_matrix.py" "$Project\scripts\production_shape_smoke.py" "$Project\scripts\firewall.sh" "$Project\deploy\provider-broker.service" "$Project\deploy\provider-broker-firewall.service" "${HostName}:$Stage/"
if ($LASTEXITCODE -ne 0) { throw "Could not upload release files" }
ssh $HostName "sudo -n bash '$Stage/install.sh' '$Version' '$Stage'"
if ($LASTEXITCODE -ne 0) { throw "Remote install failed or was rolled back" }
