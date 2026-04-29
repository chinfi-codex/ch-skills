$ErrorActionPreference = "Stop"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$Generator = Join-Path $ScriptDir "gen-skill-docs.ts"

node --experimental-strip-types $Generator
