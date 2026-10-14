# Turns selfcheck.json into GitHub annotations (readable without log access).
# GitHub keeps at most 10 notices + 10 errors per step, so passes are batched.
param([string]$ReportPath, [string]$Label)
if (-not (Test-Path $ReportPath)) {
  Write-Output "::error title=$Label selfcheck::selfcheck.json missing at $ReportPath"
  exit 1
}
$report = Get-Content $ReportPath -Raw | ConvertFrom-Json
$passed = @()
foreach ($check in $report.checks.PSObject.Properties) {
  $detail = ($check.Value.detail | ConvertTo-Json -Compress -Depth 6)
  if ($null -eq $detail) { $detail = "" }
  if ($detail.Length -gt 600) { $detail = $detail.Substring(0, 600) }
  if ($check.Value.passed) {
    $passed += $check.Name
    if ($check.Name -in @("monitors_enumerated", "point_maps_to_screen_center", "buddy_rendered_on_screen", "overlay_excluded_from_capture", "buddy_hidden_from_capture")) {
      Write-Output "::notice title=$Label $($check.Name)::PASS $detail"
    }
  } elseif ($check.Value.required) {
    Write-Output "::error title=$Label $($check.Name)::FAIL $detail"
  } else {
    Write-Output "::warning title=$Label $($check.Name)::WARN $detail"
  }
}
Write-Output "::notice title=$Label passed checks::$($passed -join ', ')"
if (-not $report.passed) { exit 1 }
