param(
    [int]$MaxIterations = 0,
    [string]$Model = "",
    [switch]$Unsafe
)

$ErrorActionPreference = "Stop"

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$repoRoot = (Resolve-Path (Join-Path $scriptDir "..\\..")).Path
$promptFile = Join-Path $scriptDir "CODEX.md"
$prdFile = Join-Path $scriptDir "prd.json"
$progressFile = Join-Path $scriptDir "progress.txt"
$archiveDir = Join-Path $scriptDir "archive"
$lastBranchFile = Join-Path $scriptDir ".last-branch"
$watchdogPollSeconds = 5
$postResultExitGraceSeconds = 300

function Reset-ProgressFile {
    $lines = @(
        "# Ralph Progress Log"
        ("Started: " + (Get-Date -Format "yyyy-MM-dd HH:mm:ss"))
        "---"
    )
    Set-Content -LiteralPath $progressFile -Value $lines
}

function Get-BranchNameFromPrd {
    if (-not (Test-Path $prdFile)) {
        return ""
    }

    try {
        $json = Get-Content -LiteralPath $prdFile -Raw | ConvertFrom-Json
        return [string]$json.branchName
    } catch {
        throw "Failed to parse $prdFile"
    }
}

function Test-AllStoriesComplete {
    if (-not (Test-Path $prdFile)) {
        return $false
    }

    $json = Get-Content -LiteralPath $prdFile -Raw | ConvertFrom-Json
    if (-not $json.userStories) {
        return $false
    }

    foreach ($story in $json.userStories) {
        if (-not $story.passes) {
            return $false
        }
    }

    return $true
}

function Archive-PreviousRunIfNeeded {
    if (-not (Test-Path $prdFile) -or -not (Test-Path $lastBranchFile)) {
        return
    }

    $currentBranch = Get-BranchNameFromPrd
    $lastBranch = (Get-Content -LiteralPath $lastBranchFile -Raw).Trim()

    if ([string]::IsNullOrWhiteSpace($currentBranch) -or [string]::IsNullOrWhiteSpace($lastBranch)) {
        return
    }

    if ($currentBranch -eq $lastBranch) {
        return
    }

    $progressHasEntries = $false
    if (Test-Path $progressFile) {
        $lineCount = (Get-Content -LiteralPath $progressFile | Measure-Object -Line).Lines
        $progressHasEntries = $lineCount -gt 3
    }

    if ($progressHasEntries) {
        $folderName = $lastBranch -replace "^ralph/", ""
        $archivePath = Join-Path $archiveDir ((Get-Date -Format "yyyy-MM-dd") + "-" + $folderName)
        New-Item -ItemType Directory -Path $archivePath -Force | Out-Null
        Copy-Item -LiteralPath $prdFile -Destination (Join-Path $archivePath "prd.json") -Force
        Copy-Item -LiteralPath $progressFile -Destination (Join-Path $archivePath "progress.txt") -Force
    }

    Reset-ProgressFile
}

function Resolve-CodexLaunchPath {
    $cmdShim = Get-Command "codex.cmd" -ErrorAction SilentlyContinue
    if ($null -ne $cmdShim) {
        return $cmdShim.Source
    }

    $codexCommand = Get-Command "codex" -ErrorAction SilentlyContinue
    if ($null -eq $codexCommand) {
        throw "Could not resolve a runnable Codex command."
    }

    if ($codexCommand.CommandType -eq "Application") {
        return $codexCommand.Source
    }

    $cmdSibling = [System.IO.Path]::ChangeExtension($codexCommand.Source, ".cmd")
    if (Test-Path $cmdSibling) {
        return $cmdSibling
    }

    throw ("Resolved Codex command '{0}' is not directly runnable by Start-Process." -f $codexCommand.Source)
}

function Stop-ProcessTree {
    param(
        [int]$ProcessId
    )

    $children = @(Get-CimInstance Win32_Process -Filter ("ParentProcessId = {0}" -f $ProcessId) -ErrorAction SilentlyContinue)
    foreach ($child in $children) {
        Stop-ProcessTree -ProcessId $child.ProcessId
    }

    Stop-Process -Id $ProcessId -Force -ErrorAction SilentlyContinue
}

function Remove-FileWithRetry {
    param(
        [string]$LiteralPath,
        [int]$MaxAttempts = 10,
        [int]$DelayMilliseconds = 500
    )

    for ($attempt = 1; $attempt -le $MaxAttempts; $attempt++) {
        if (-not (Test-Path $LiteralPath)) {
            return $true
        }

        try {
            Remove-Item -LiteralPath $LiteralPath -Force -ErrorAction Stop
            return $true
        } catch [System.IO.IOException] {
            if ($attempt -eq $MaxAttempts) {
                Write-Warning ("Could not remove temp file after {0} attempts: {1}" -f $MaxAttempts, $LiteralPath)
                return $false
            }

            Start-Sleep -Milliseconds $DelayMilliseconds
        } catch {
            Write-Warning ("Failed to remove temp file '{0}': {1}" -f $LiteralPath, $_.Exception.Message)
            return $false
        }
    }

    return $false
}

function Invoke-CodexIteration {
    param(
        [string]$CodexLaunchPath,
        [string[]]$CodexArgs,
        [string]$PromptText,
        [string]$LastMessageFile
    )

    $promptInputFile = Join-Path $env:TEMP ("codex-ralph-stdin-" + [guid]::NewGuid().ToString("N") + ".txt")
    $process = $null
    $lastMessageWriteSeenUtc = [datetime]::MinValue
    $lastMessageDetectedAt = $null

    try {
        Set-Content -LiteralPath $promptInputFile -Value $PromptText -Encoding UTF8 -NoNewline
        $process = Start-Process -FilePath $CodexLaunchPath -ArgumentList $CodexArgs -RedirectStandardInput $promptInputFile -NoNewWindow -PassThru

        while (-not $process.HasExited) {
            Start-Sleep -Seconds $watchdogPollSeconds
            $process.Refresh()
            if ($process.HasExited) {
                break
            }

            if (-not (Test-Path $LastMessageFile)) {
                continue
            }

            $messageFileInfo = Get-Item -LiteralPath $LastMessageFile
            if ($messageFileInfo.Length -le 0) {
                continue
            }

            $messageWriteTimeUtc = $messageFileInfo.LastWriteTimeUtc
            if ($messageWriteTimeUtc -gt $lastMessageWriteSeenUtc) {
                $lastMessageWriteSeenUtc = $messageWriteTimeUtc
                $lastMessageDetectedAt = Get-Date
                Write-Host ("Codex iteration wrote its final message. Waiting up to {0} seconds for a clean exit..." -f $postResultExitGraceSeconds)
                continue
            }

            if ($null -eq $lastMessageDetectedAt) {
                continue
            }

            $elapsedSinceLastMessage = ((Get-Date) - $lastMessageDetectedAt).TotalSeconds
            if ($elapsedSinceLastMessage -lt $postResultExitGraceSeconds) {
                continue
            }

            Write-Host ("Codex iteration appears stuck after writing results. Stopping process tree rooted at PID {0} and continuing..." -f $process.Id)
            Stop-ProcessTree -ProcessId $process.Id
            break
        }

        if ($null -ne $process) {
            try {
                $process.WaitForExit()
            } catch {
            }
        }
    } finally {
        Remove-FileWithRetry -LiteralPath $promptInputFile | Out-Null
    }
}

if (-not (Test-Path $promptFile)) {
    throw "Missing prompt file: $promptFile"
}

if (-not (Test-Path $progressFile)) {
    Reset-ProgressFile
}

Archive-PreviousRunIfNeeded

$branchName = Get-BranchNameFromPrd
if (-not [string]::IsNullOrWhiteSpace($branchName)) {
    Set-Content -LiteralPath $lastBranchFile -Value $branchName
}

if (-not (Test-Path $prdFile)) {
    throw "Missing $prdFile. Generate it first with the local ralph skill."
}

$runUntilComplete = $MaxIterations -le 0
$iteration = 1
$iterationLabel = if ($runUntilComplete) { "until complete" } else { [string]$MaxIterations }
$codexLaunchPath = Resolve-CodexLaunchPath

Write-Host ("Starting Codex Ralph - Max iterations: {0}" -f $iterationLabel)

while ($true) {
    if (-not $runUntilComplete -and $iteration -gt $MaxIterations) {
        break
    }

    Write-Host ""
    Write-Host "==============================================================="
    if ($runUntilComplete) {
        Write-Host ("  Codex Ralph Iteration {0} (until complete)" -f $iteration)
    } else {
        Write-Host ("  Codex Ralph Iteration {0} of {1}" -f $iteration, $MaxIterations)
    }
    Write-Host "==============================================================="

    $lastMessageFile = Join-Path $env:TEMP ("codex-ralph-last-message-" + [guid]::NewGuid().ToString("N") + ".txt")
    $modeArgs = if ($Unsafe) { @("--dangerously-bypass-approvals-and-sandbox") } else { @("--full-auto") }
    $codexArgs = @(
        "exec"
        "-C", $repoRoot
    ) + $modeArgs + @(
        "--output-last-message", $lastMessageFile
        "-"
    )

    if (-not [string]::IsNullOrWhiteSpace($Model)) {
        $codexArgs = @("exec", "-C", $repoRoot, "-m", $Model) + $modeArgs + @("--output-last-message", $lastMessageFile, "-")
    }

    $promptText = Get-Content -LiteralPath $promptFile -Raw
    Invoke-CodexIteration -CodexLaunchPath $codexLaunchPath -CodexArgs $codexArgs -PromptText $promptText -LastMessageFile $lastMessageFile

    $lastMessage = ""
    if (Test-Path $lastMessageFile) {
        $lastMessage = Get-Content -LiteralPath $lastMessageFile -Raw
        Remove-FileWithRetry -LiteralPath $lastMessageFile | Out-Null
    }

    $allStoriesComplete = Test-AllStoriesComplete
    if ($allStoriesComplete) {
        Write-Host ""
        Write-Host "Codex Ralph completed all tasks."
        exit 0
    }

    if ($lastMessage -match "<promise>COMPLETE</promise>") {
        Write-Warning "Codex iteration reported COMPLETE before all stories passed. Ignoring the marker and continuing..."
    }

    Write-Host ("Iteration {0} complete. Continuing..." -f $iteration)
    Start-Sleep -Seconds 2
    $iteration++
}

Write-Host ""
Write-Host ("Codex Ralph reached max iterations ({0}) without completing all tasks." -f $MaxIterations)
Write-Host ("Check {0} for progress details." -f $progressFile)
exit 1
