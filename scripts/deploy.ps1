param([string]$HostName = "yosef-server")
$ErrorActionPreference = "Stop"
$Project = Split-Path -Parent $PSScriptRoot
ssh $HostName "echo yosef | sudo -S install -d -o yosef -g yosef /data/provider-broker/stage"
if ($LASTEXITCODE -ne 0) { throw "Could not create remote staging directory" }
scp -r "$Project\*" "${HostName}:/data/provider-broker/stage/"
if ($LASTEXITCODE -ne 0) { throw "Could not copy project to server" }
ssh $HostName "cd /data/provider-broker/stage && echo yosef | sudo -S bash scripts/install.sh"
if ($LASTEXITCODE -ne 0) { throw "Remote install failed" }
