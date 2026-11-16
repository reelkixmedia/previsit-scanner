<#
Previsit office agent. Sends the newest exports from a folder to Previsit, so nobody has to upload by hand.

Set the practice software's scheduled reports to save into one folder, with names containing
"appointments", "medications" and "labs" (and "patients" for a separate contact-details report).
Comma, tab and pipe separated files all work. Then run this from Windows Task Scheduler each night:

  powershell -NoProfile -ExecutionPolicy Bypass -File previsit_agent.ps1 -Folder "C:\PrevisitExports" -Server "https://your-previsit-site" -KeyFile "C:\PrevisitExports\upload.key"

Sent files move to <Folder>\sent\<date>. Add -DeleteAfterSend to remove them instead (better for patient privacy).
Results are written to <Folder>\previsit_agent.log, which holds no patient information.
#>
param(
    [Parameter(Mandatory = $true)][string]$Folder,
    [Parameter(Mandatory = $true)][string]$Server,
    [Parameter(Mandatory = $true)][string]$KeyFile,
    [switch]$DeleteAfterSend
)

$log = Join-Path $Folder "previsit_agent.log"
function Write-Log($message) {
    Add-Content -Path $log -Value "$(Get-Date -Format s) $message"
}

if ($Server -notmatch '^https://' -and $Server -notmatch '^http://(localhost|127\.0\.0\.1)(:|/|$)') {
    Write-Log "REFUSED: server must use https"
    exit 2
}
$key = (Get-Content -Path $KeyFile -TotalCount 1).Trim()

$files = @{}
foreach ($kind in "appointments", "medications", "labs", "patients") {
    $newest = Get-ChildItem -Path $Folder -Filter "*$kind*" -File |
        Where-Object { $_.Extension -in ".csv", ".tsv", ".txt" } |
        Sort-Object LastWriteTime -Descending | Select-Object -First 1
    if ($newest) { $files[$kind] = $newest }
}
if (-not $files.ContainsKey("appointments")) {
    Write-Log "NOTHING SENT: no appointments file found"
    exit 3
}

# The key goes in a temporary header file so it never appears in the process list.
$headerFile = [System.IO.Path]::GetTempFileName()
Set-Content -Path $headerFile -Value "X-Previsit-Key: $key" -Encoding Ascii
$curlArgs = @("--silent", "--show-error", "--max-time", "120", "--output", "NUL", "--write-out", "%{http_code}", "--header", "@$headerFile")
foreach ($kind in $files.Keys) {
    $curlArgs += @("--form", "$kind=@$($files[$kind].FullName);type=text/csv")
}
$curlArgs += "$($Server.TrimEnd('/'))/api/v1/import"
$status = & curl.exe @curlArgs
Remove-Item -Path $headerFile -Force

if ($status -ne "200") {
    Write-Log "FAILED: server said $status. Files were kept. Check Previsit for a column-matching prompt."
    exit 1
}
if ($DeleteAfterSend) {
    $files.Values | Remove-Item -Force
} else {
    $done = Join-Path $Folder ("sent\" + (Get-Date -Format "yyyy-MM-dd_HHmmss"))
    New-Item -ItemType Directory -Path $done -Force | Out-Null
    $files.Values | Move-Item -Destination $done
}
Write-Log "OK: sent $($files.Count) files"
exit 0
